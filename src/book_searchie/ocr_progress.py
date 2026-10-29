"""An OCRmyPDF plugin that passes page-by-page progress to a callback instead of drawing a bar.

Loaded by ocr.ocr_pdf only when a progress callback is wanted (the web UI's OCR page).
"""

from __future__ import annotations

from collections.abc import Callable

from ocrmypdf import hookimpl

# Set by ocr.ocr_pdf before each run: callback(step, pages_done, pages_total).
callback: Callable[[str, float, float | None], None] | None = None


class _Reporter:
    def __init__(self, *, total=None, desc=None, unit=None, disable=False, **kwargs):
        self.total = total
        self.desc = desc or ""
        self.unit = unit
        self.done = 0.0

    def __enter__(self):
        self._emit()
        return self

    def __exit__(self, *exc):
        return False

    def update(self, n=1, *, completed=None):
        self.done = completed if completed is not None else self.done + (n or 0)
        self._emit()

    def _emit(self):
        if callback is not None and self.unit == "page":
            callback(self.desc, self.done, self.total)


@hookimpl
def get_progressbar_class():
    return _Reporter
