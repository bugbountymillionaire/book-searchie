"""Books that can't be searched: what each problem means and how to fix it.

Shared by the 'report' command and the web UI's "Can't search" page.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Problem:
    title: str
    summary: str  # short phrase for one-line summaries
    explanation: str
    fix: str


# In the order they're reported.
PROBLEMS = {
    "no_text": Problem(
        "Scanned books",
        "scanned (fix: book-searchie ocr)",
        "The pages are pictures of text, so there are no words to search.",
        "Run OCR to read the text from the page images. Your files aren't changed: an OCR'd copy "
        "is kept next to the index. It takes roughly 1-3 seconds per page.",
    ),
    "ocr_empty": Problem(
        "Scanned books where OCR found no text",
        "scanned, OCR found no text",
        "OCR ran but couldn't read any words. These may be photos, handwriting or very poor scans.",
        "Try a better copy of the book, or OCR again in the book's language, e.g. "
        "book-searchie ocr --language deu \"Topic/Book.pdf\".",
    ),
    "password": Problem(
        "Password-protected PDFs",
        "password-protected",
        "The PDF needs a password just to open it.",
        "Remove the password once and replace the file: open it, enter the password and "
        "'Print to PDF', or run: qpdf --decrypt --password=PASSWORD locked.pdf unlocked.pdf",
    ),
    "drm": Problem(
        "DRM-protected books",
        "DRM-protected",
        "The shop that sold the book encrypted it so only its own reading app can open it.",
        "Get a DRM-free copy. Many publishers and shops sell them, and older books are often free "
        "from Project Gutenberg or Standard Ebooks. Meanwhile, use the reading app's own search.",
    ),
    "error": Problem(
        "Damaged or unreadable files",
        "damaged or unreadable",
        "The file is corrupt, or isn't really a PDF or EPUB.",
        "Re-download or replace the file. The reason is shown next to each one.",
    ),
}


def problem_kind(row) -> str:
    if row["status"] == "no_text":
        if row["ocr"]:
            return "ocr_empty"
        if row["kind"] != "pdf":
            return "error"  # an EPUB with no text; OCR can't help
    return row["status"]


def group_problems(rows) -> dict[str, list]:
    """Problem rows grouped by kind, in report order, leaving out empty groups."""
    groups: dict[str, list] = {kind: [] for kind in PROBLEMS}
    for row in rows:
        groups[problem_kind(row)].append(row)
    return {kind: rows for kind, rows in groups.items() if rows}


def detail(kind: str, row) -> str:
    """Extra detail shown next to a book: page count, DRM vendor or error reason."""
    if kind == "error":
        return row["message"] or "no text found"
    if kind == "drm":
        message = row["message"] or ""
        return message.removeprefix("DRM protected (").rstrip(")") if "(" in message else ""
    if kind in ("no_text", "ocr_empty"):
        return f"{row['units']} pages"
    return ""
