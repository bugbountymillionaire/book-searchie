"""Building and incrementally updating the index from the library."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from .config import Config
from .db import Database
from .extract import ExtractError, extract
from .scan import scan_library


@dataclass
class IndexReport:
    added: int = 0
    updated: int = 0
    removed: int = 0
    unchanged: int = 0
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
    for path, state in known.items():
        if path not in found and state.root not in report.missing_roots:
            db.remove_file(state.id)
            report.removed += 1

    todo = []
    for path, f in found.items():
        state = known.get(path)
        if state is not None and state.size == f.size and state.mtime == f.mtime:
            report.unchanged += 1
        else:
            todo.append((f, state is None))

    for i, (f, is_new) in enumerate(todo, 1):
        progress(f"[{i}/{len(todo)}] {f.rel}")
        try:
            db.store_file(f, extract(f.path, f.kind))
        except ExtractError as e:
            db.store_file(f, None, error=str(e))
        except Exception as e:  # never let one bad book stop the whole run
            db.store_file(f, None, error=f"{type(e).__name__}: {e}")
        if is_new:
            report.added += 1
        else:
            report.updated += 1
    return report
