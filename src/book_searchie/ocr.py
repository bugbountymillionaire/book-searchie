"""Making scanned PDFs searchable with OCRmyPDF, without touching the originals.

OCR'd copies live in a cache folder next to the index, named by the original's
SHA-256, so they survive the book being renamed or moved and are ignored once
the book itself changes.
"""

from __future__ import annotations

import hashlib
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .db import Database

INSTALL_HELP = """\
OCR needs the optional OCRmyPDF package plus the Tesseract OCR program.

  1. In Book-Searchie's environment:  pip install "book-searchie[ocr]"
     (from a source checkout:         pip install -e ".[ocr]")
  2. Tesseract:
     Windows:  winget install UB-Mannheim.TesseractOCR
     macOS:    brew install tesseract
     Linux:    sudo apt install tesseract-ocr   (or your distro's equivalent)
"""


class OcrUnavailable(Exception):
    """OCRmyPDF or one of the programs it needs isn't installed."""


class OcrFailed(Exception):
    """OCR of one book failed."""


def ocr_dir(config: Config) -> Path:
    return config.index_path.parent / f"{config.index_path.stem}-ocr"


def ocr_copy_path(config: Config, content_hash: str) -> Path:
    return ocr_dir(config) / f"{content_hash}.pdf"


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check_available() -> None:
    try:
        import ocrmypdf  # noqa: F401
    except ImportError:
        raise OcrUnavailable("OCRmyPDF isn't installed.\n\n" + INSTALL_HELP) from None


def ocr_pdf(src: Path, dest: Path, language: str, *, progress_bar: bool = False) -> None:
    """OCR src into dest. Pages that already have text are left as they are."""
    import ocrmypdf
    from ocrmypdf.exceptions import ExitCodeException, MissingDependencyError

    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_suffix(".partial.pdf")
    try:
        ocrmypdf.ocr(
            src,
            partial,
            language=language.split("+"),
            skip_text=True,  # keep pages that already have text, OCR only the image pages
            output_type="pdf",  # plain PDF; we don't need PDF/A
            progress_bar=progress_bar,
        )
        os.replace(partial, dest)
    except MissingDependencyError as e:
        raise OcrUnavailable(f"{e}\n\n{INSTALL_HELP}") from None
    except ExitCodeException as e:
        raise OcrFailed(str(e) or type(e).__name__) from None
    finally:
        partial.unlink(missing_ok=True)


@dataclass
class OcrReport:
    done: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)


def ocr_candidates(db: Database, only: list[str] | None = None) -> list:
    """Scanned PDFs without an OCR copy, optionally narrowed to the named books.

    Books can be named by their path as shown in the report (Topic/Book.pdf) or by full path.
    Raises ValueError listing any names that aren't scanned books.
    """
    rows = [r for r in db.problem_files() if r["status"] == "no_text" and r["kind"] == "pdf" and not r["ocr"]]
    if not only:
        return rows
    wanted = {}
    for name in only:
        key = name.replace("\\", "/").strip("/").lower()
        wanted[key] = name
        wanted[str(Path(name).resolve()).lower()] = name
    chosen = [r for r in rows if r["rel"].lower() in wanted or r["path"].lower() in wanted]
    matched = {wanted.get(r["rel"].lower()) or wanted.get(r["path"].lower()) for r in chosen}
    unknown = [n for n in only if n not in matched]
    if unknown:
        raise ValueError(
            "Not a scanned book waiting for OCR: " + ", ".join(unknown)
            + ". Run 'book-searchie report' to see the list."
        )
    return chosen


def run_ocr(
    config: Config,
    rows: list,
    *,
    language: str = "eng",
    progress: Callable[[str], None] = lambda msg: None,
) -> OcrReport:
    check_available()
    report = OcrReport()
    show_bar = sys.stderr.isatty()
    for i, row in enumerate(rows, 1):
        progress(f"[{i}/{len(rows)}] {row['rel']} ({row['units']} pages)")
        try:
            ocr_pdf(Path(row["path"]), ocr_copy_path(config, row["content_hash"]), language, progress_bar=show_bar)
            report.done.append(row["rel"])
        except OcrFailed as e:
            report.failed.append((row["rel"], str(e)))
    return report
