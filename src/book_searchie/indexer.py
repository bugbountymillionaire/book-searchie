"""Building and incrementally updating the index from the library."""

from __future__ import annotations

import os
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .db import Database, FileState
from .extract import DrmProtected, ExtractError, PasswordProtected, extract, extract_pdf
from .ocr import file_hash, ocr_copy_path
from .scan import LibraryFile, scan_library


@dataclass
class IndexReport:
    added: int = 0
    updated: int = 0
    removed: int = 0
    moved: int = 0  # moved or renamed, recognised without reading them again
    unchanged: int = 0
    duplicates: int = 0  # copies of a book in another format, not searched
    missing_roots: list[str] = field(default_factory=list)


def run_index(
    config: Config,
    db: Database,
    *,
    full: bool = False,
    progress: Callable[[str], None] = lambda msg: None,
) -> IndexReport:
    report = IndexReport()
    if full:
        db.clear()

    roots = []
    for root in config.roots:
        if root.is_dir():
            roots.append(root)
        else:
            report.missing_roots.append(str(root))
    if report.missing_roots and not roots:
        # Every root is unreachable (unplugged drive, unmounted share). Don't wipe the index.
        return report

    known = db.file_states()
    found = {str(f.path): f for f in scan_library(roots, config.extensions, config.exclude)}

    # Files under a missing root are kept, in case the drive comes back.
    gone = {p: s for p, s in known.items() if p not in found and s.root not in report.missing_roots}
    arrived = [f for p, f in found.items() if p not in known]
    moved = set()
    for f, old_path in _match_moves(gone, arrived):
        state = gone.pop(old_path)
        db.move_file(state.id, f)
        known[str(f.path)] = known.pop(old_path)
        moved.add(str(f.path))
    report.moved = len(moved)
    for state in gone.values():
        db.remove_file(state.id)
        report.removed += 1

    def unchanged(f: LibraryFile) -> None:
        if str(f.path) not in moved:  # those are counted as moved
            report.unchanged += 1

    def needs_indexing(f: LibraryFile, state: FileState | None) -> bool:
        return (
            state is None or state.size != f.size or state.mtime != f.mtime
            or state.status == "duplicate"  # it was skipped, so its text was never read
            or _ocr_changed(config, state)
        )

    # The same book in several formats in one folder ("Herbs.pdf", "Herbs.epub") is
    # searched once: the preferred format, or failing that the next one that works.
    rank = [config.prefer] + [k for k in ("pdf", "epub") if k != config.prefer]
    books: dict[tuple[str, str], list[LibraryFile]] = defaultdict(list)
    for f in found.values():
        books[_book_key(f)].append(f)

    # For progress. Copies skipped last time usually stay skipped, so they aren't counted.
    total = sum(
        needs_indexing(f, s) and not (s and s.status == "duplicate" and s.size == f.size and s.mtime == f.mtime)
        for f, s in ((f, known.get(path)) for path, f in found.items())
    )
    done = 0
    for copies in books.values():
        copies.sort(key=lambda f: rank.index(f.kind))
        used: LibraryFile | None = None  # the copy being searched
        for f in copies:
            state = known.get(str(f.path))
            if used is not None:
                report.duplicates += 1
                if state and state.status == "duplicate" and state.size == f.size and state.mtime == f.mtime:
                    unchanged(f)
                    continue
                db.store_duplicate(f, used.path.name)
            elif needs_indexing(f, state):
                done += 1
                progress(f"[{done}/{max(done, total)}] {f.rel}")
                _index_file(config, db, f)
                if db.status_of(f.path) == "ok":
                    used = f
            else:
                unchanged(f)
                if state.status == "ok":
                    used = f
                continue
            if state is None:
                report.added += 1
            else:
                report.updated += 1
    return report


def _match_moves(gone: dict[str, FileState], arrived: list[LibraryFile]) -> list[tuple[LibraryFile, str]]:
    """Pair new files with vanished ones that are the same file, moved or renamed.

    Moving or renaming a file keeps its size and modified time, so a new file that matches
    a vanished one on both (and on file type) is taken to be it. If several vanished files
    match, the one with the same name is used; if that doesn't settle it, the file is
    simply read again, so a wrong guess can't happen.
    """
    candidates: dict[tuple, list[str]] = defaultdict(list)
    for path, state in gone.items():
        candidates[state.size, state.mtime, Path(path).suffix.lower()].append(path)
    pairs, taken = [], set()
    for f in arrived:
        options = [p for p in candidates.get((f.size, f.mtime, f.path.suffix.lower()), ()) if p not in taken]
        if len(options) > 1:
            options = [p for p in options if Path(p).name.casefold() == f.path.name.casefold()]
        if len(options) == 1:
            pairs.append((f, options[0]))
            taken.add(options[0])
    return pairs


def _book_key(f: LibraryFile) -> tuple[str, str]:
    """Files with the same name in the same folder, ignoring case and extension."""
    return os.path.normcase(str(f.path.parent)), f.path.stem.casefold()


def _ocr_changed(config: Config, state: FileState) -> bool:
    """True if an OCR copy has appeared for a scanned book, or the one in use has gone."""
    if not state.content_hash:
        return False
    return ocr_copy_path(config, state.content_hash).is_file() != state.ocr


def _index_file(config: Config, db: Database, f: LibraryFile) -> None:
    try:
        extracted = extract(f.path, f.kind)
    except PasswordProtected as e:
        return db.store_file(f, None, status="password", message=str(e))
    except DrmProtected as e:
        return db.store_file(f, None, status="drm", message=str(e))
    except ExtractError as e:
        return db.store_file(f, None, status="error", message=str(e))
    except Exception as e:  # never let one bad book stop the whole run
        return db.store_file(f, None, status="error", message=f"{type(e).__name__}: {e}")

    if not extracted.no_text:
        return db.store_file(f, extracted, status="ok")
    if f.kind != "pdf":
        return db.store_file(f, extracted, status="no_text")

    # A scanned PDF: use its OCR copy if there is one.
    digest = file_hash(f.path)
    copy = ocr_copy_path(config, digest)
    if copy.is_file():
        try:
            from_copy = extract_pdf(copy)
        except ExtractError:
            pass  # damaged copy; fall back to the original and let 'ocr' redo it
        else:
            status = "no_text" if from_copy.no_text else "ok"
            return db.store_file(f, from_copy, status=status, content_hash=digest, ocr=True)
    db.store_file(f, extracted, status="no_text", content_hash=digest)
