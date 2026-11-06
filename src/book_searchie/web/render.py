"""Rendering book pages for the viewer: PDF pages as highlighted images, EPUB chapters as safe HTML."""

from __future__ import annotations

import html
import posixpath
import re
import threading
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote

import pymupdf

from ..extract import _BLOCK_TAGS, _Epub, _resolve
from ..highlight import Term, find_spans, fold, text_spans

# PyMuPDF isn't thread-safe, and the web server handles requests on several threads.
_pdf_lock = threading.Lock()

_WORD = re.compile(r"\w+")

# Highlight colour for matches on PDF pages (yellow, like a highlighter pen).
_HIGHLIGHT = (1.0, 0.85, 0.1)


@dataclass
class PdfPage:
    page_count: int
    label: str
    width: float  # in points
    height: float
    # Each match is one or more boxes (one per line it spans), as fractions of the
    # page size, so the viewer can place them over the image at any zoom.
    matches: list[list[tuple[float, float, float, float]]]


def pdf_page(path: Path, seq: int, terms: list[Term]) -> PdfPage:
    """What the viewer needs to know about one page, including where the matches are."""
    with _pdf_lock, pymupdf.open(path) as doc:
        if not 0 <= seq < doc.page_count:
            raise IndexError(seq)
        page = doc[seq]
        w, h = page.rect.width, page.rect.height
        matches = []
        for boxes in _match_boxes(page, terms):
            matches.append([(r.x0 / w, r.y0 / h, r.x1 / w, r.y1 / h) for r in boxes])
        return PdfPage(doc.page_count, page.get_label() or str(seq + 1), w, h, matches)


def render_pdf_page(path: Path, seq: int, terms: list[Term], *, dpi: int = 150) -> tuple[bytes, int]:
    """PNG of one page with matching words highlighted, and how many matches were highlighted."""
    with _pdf_lock, pymupdf.open(path) as doc:
        if not 0 <= seq < doc.page_count:
            raise IndexError(seq)
        page = doc[seq]
        matches = _match_boxes(page, terms)
        for boxes in matches:
            for rect in boxes:
                annot = page.add_highlight_annot(rect)
                annot.set_colors(stroke=_HIGHLIGHT)
                annot.update()
        # The document is only changed in memory; nothing is saved.
        return page.get_pixmap(dpi=dpi, annots=True).tobytes("png"), len(matches)


def _match_boxes(page, terms: list[Term]) -> list[list[pymupdf.Rect]]:
    """Each match on the page, in reading order, as one box per line it covers."""
    if not terms:
        return []
    # Each word is (x0, y0, x1, y1, text, block, line, word number).
    # Same order the indexer reads the text in, which follows columns.
    words = page.get_text("words", flags=pymupdf.TEXTFLAGS_WORDS)
    lines = _line_metrics(page)
    tokens, owners, places = [], [], []  # places: where each token is in its word's text
    word_tokens: dict[tuple, list[int]] = {}  # owner -> its tokens
    i = 0
    while i < len(words):
        text, owner = words[i][4], (i,)
        nxt = words[i + 1] if i + 1 < len(words) else None
        # Rejoin a word hyphenated across lines ("co-" + "hosh"), as the indexer does.
        if text.endswith("-") and nxt and nxt[5:7] != words[i][5:7] and nxt[4][:1].islower():
            text, owner = text[:-1] + nxt[4], (i, i + 1)
            i += 1
        for m in _WORD.finditer(text):
            word_tokens.setdefault(owner, []).append(len(tokens))
            tokens.append(fold(m.group()))
            owners.append(owner)
            places.append((m.start(), m.end()))
        i += 1
    textpage = None  # built on first use, for finding parts of words
    matches = []
    for start, end in find_spans(tokens, terms):
        # Merge the match's words into one box per line, so a phrase reads as one highlight.
        boxes: dict[tuple, pymupdf.Rect] = {}
        for owner in dict.fromkeys(owners[k] for k in range(start, end)):
            for w in owner:
                key = words[w][5:7]
                rect = pymupdf.Rect(words[w][:4])
                inside = [k for k in word_tokens[owner] if start <= k < end]
                text = words[w][4]
                part = (places[inside[0]][0], places[inside[-1]][1])
                if len(owner) == 1 and part != (0, len(text)):
                    # Only part of the word matches: "Dovetail" in "Dovetail.—Fig.", or a
                    # word with punctuation attached ("dovetail.").
                    textpage = textpage or page.get_textpage(flags=pymupdf.TEXTFLAGS_SEARCH)
                    rect = _part_of_word(page, textpage, rect, text, *part)
                rect = _fit_to_text(rect, lines.get(key))
                boxes[key] = boxes[key] | rect if key in boxes else rect
        matches.append(list(boxes.values()))
    return matches


def _part_of_word(page, textpage, word: pymupdf.Rect, text: str, start: int, end: int) -> pymupdf.Rect:
    """The box around text[start:end] within a word, or the whole word if it can't be found."""
    part = text[start:end]
    # search_for ignores its clip when given a textpage, so keep the hits inside this word ourselves.
    area = word + (-1, -1, 1, 1)
    hits = sorted(
        (h for h in page.search_for(part, textpage=textpage) if area.contains(pymupdf.Point((h.x0 + h.x1) / 2, (h.y0 + h.y1) / 2))),
        key=lambda h: h.x0,
    )
    # The part may occur more than once in the word ("dovetail-dovetail"); pick the right one.
    nth = text.lower().count(part.lower(), 0, start)
    return hits[nth] if nth < len(hits) else word


def _line_metrics(page) -> dict[tuple[int, int], tuple[float, float, float, float]]:
    """(baseline, font size, ascender, descender) for each (block, line) of words."""
    metrics = {}
    # The same flags as the word list, so block and line numbers line up.
    for b, block in enumerate(page.get_text("dict", flags=pymupdf.TEXTFLAGS_WORDS)["blocks"]):
        for n, line in enumerate(block.get("lines", ())):
            if tuple(line["dir"]) == (1.0, 0.0) and line["spans"]:  # horizontal text only
                span = max(line["spans"], key=lambda s: s["size"])
                metrics[b, n] = (span["origin"][1], span["size"], span["ascender"], span["descender"])
    return metrics


def _fit_to_text(rect: pymupdf.Rect, metrics) -> pymupdf.Rect:
    # Some fonts (Cambria, notably) report boxes several lines tall, which would paint
    # big blobs over neighbouring lines. Rebuild those from the baseline and font size.
    if metrics is None:
        return rect
    baseline, size, ascender, descender = metrics
    if size > 0 and rect.height > 1.5 * size:
        return pymupdf.Rect(rect.x0, baseline - ascender * size, rect.x1, baseline - descender * size)
    return rect


# --- EPUB --------------------------------------------------------------------

_ALLOWED = {
    "p", "div", "span", "section", "article", "aside", "header", "footer", "main",
    "h1", "h2", "h3", "h4", "h5", "h6", "em", "i", "strong", "b", "u", "s", "small", "sub", "sup",
    "blockquote", "pre", "code", "ul", "ol", "li", "dl", "dt", "dd", "table", "thead", "tbody",
    "tfoot", "tr", "td", "th", "caption", "figure", "figcaption", "hr", "br", "img", "cite", "q",
}
_VOID = {"br", "hr", "img"}
# Elements whose contents are never shown.
_DROP = {"head", "script", "style", "title", "iframe", "object", "embed", "form", "noscript", "svg", "math", "template"}
IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp"}


class _Sanitizer(HTMLParser):
    """Re-emits an EPUB chapter keeping only plain formatting tags, with no attributes
    except image sources (rewritten to our asset route) and alt text. Matches are wrapped
    in numbered <mark> elements for the viewer's next/previous buttons.

    Text is matched a block (paragraph, heading, ...) at a time, joined across inline
    elements the way the indexer joins it. Some EPUBs (often ones converted from PDFs)
    split words into pieces for spacing, e.g. <span>N</span><span>ETTL</span>E. A match
    split like that is marked in each piece; the first piece carries the match's id, and
    every piece has data-m with its number, so the viewer treats them as one match."""

    def __init__(self, terms: list[Term], asset_url, chapter_dir: str):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.terms = terms
        self.asset_url = asset_url
        self.chapter_dir = chapter_dir
        self.marks = 0
        self._drop = 0
        self._open: list[str] = []
        self._block: list[tuple[bool, str]] = []  # (is text, html or raw text) since the last block boundary

    def _emit(self, markup: str) -> None:
        self._block.append((False, markup))

    def _flush(self) -> None:
        """Mark the matches in the text gathered since the last block boundary, and output it."""
        texts = [t for is_text, t in self._block if is_text]
        spans = text_spans("".join(texts), self.terms) if self.terms and texts else []
        offset = 0
        for is_text, item in self._block:
            if not is_text:
                self.out.append(item)
                continue
            pos, end = 0, len(item)
            for n, (start, stop) in enumerate(spans):
                a, b = max(start - offset, 0), min(stop - offset, end)
                if a >= b:
                    continue
                number = self.marks + n
                whole = start - offset >= 0 and stop - offset <= end
                attrs = f'id="m{number}"' if whole else (
                    f'id="m{number}" data-m="{number}"' if start - offset >= 0 else f'data-m="{number}"')
                self.out.append(html.escape(item[pos:a]))
                self.out.append(f"<mark {attrs}>{html.escape(item[a:b])}</mark>")
                pos = b
            self.out.append(html.escape(item[pos:]))
            offset += end
        self.marks += len(spans)
        self._block = []

    def handle_starttag(self, tag, attrs):
        if tag in _BLOCK_TAGS:
            self._flush()
        if tag in _DROP:
            self._drop += 1
            return
        if self._drop or tag not in _ALLOWED:
            return
        if tag == "img":
            attrs = dict(attrs)
            src = attrs.get("src") or ""
            name = _resolve(self.chapter_dir, src)
            if posixpath.splitext(name)[1].lower() in IMAGE_TYPES and "://" not in src:
                alt = html.escape(attrs.get("alt") or "", quote=True)
                # The size, when the book gives it, saves room for the picture before it
                # loads, so the text below doesn't jump down after we've scrolled to a match.
                size = "".join(f' {k}="{int(v)}"' for k in ("width", "height")
                               if (v := (attrs.get(k) or "").strip().removesuffix("px")).isdigit() and 0 < int(v) < 100000)
                self._emit(f'<img src="{html.escape(self.asset_url(name), quote=True)}" alt="{alt}"{size} loading="lazy">')
            return
        self._emit(f"<{tag}>")
        if tag not in _VOID:
            self._open.append(tag)

    def handle_startendtag(self, tag, attrs):  # XHTML's <tag/>
        if tag in _DROP:
            return
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in _BLOCK_TAGS:
            self._flush()
        if tag in _DROP:
            self._drop = max(0, self._drop - 1)
            return
        if self._drop or tag not in self._open:
            return
        while self._open:
            open_tag = self._open.pop()
            self._emit(f"</{open_tag}>")
            if open_tag == tag:
                break

    def handle_data(self, data):
        if self._drop:
            return
        self._block.append((True, data))

    def close(self):
        super().close()
        self._flush()

    def result(self) -> str:
        return "".join(self.out) + "".join(f"</{t}>" for t in reversed(self._open))


def render_epub_chapter(path: Path, href: str, terms: list[Term], asset_url) -> tuple[str, int]:
    """Safe HTML for one chapter, and how many matches it marks."""
    with zipfile.ZipFile(path) as zf:
        source = _Epub(zf).read_text(href)
    parser = _Sanitizer(terms, asset_url, posixpath.dirname(href))
    parser.feed(source)
    parser.close()
    return parser.result(), parser.marks


def read_epub_image(path: Path, name: str) -> tuple[bytes, str]:
    """An image from inside an EPUB. Raises KeyError if it's missing or not an image."""
    media_type = IMAGE_TYPES.get(posixpath.splitext(name)[1].lower())
    if media_type is None:
        raise KeyError(name)
    with zipfile.ZipFile(path) as zf:
        return _Epub(zf).read(name), media_type


def asset_url_for(file_id: int):
    return lambda name: f"/asset/{file_id}?name={quote(name)}"
