"""Extracting searchable text from PDFs (one section per page) and EPUBs (one per chapter)."""

from __future__ import annotations

import posixpath
import re
import unicodedata
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote
from xml.etree import ElementTree as ET

import pymupdf

# Below this many characters per page on average, a PDF is treated as scanned (no text layer).
MIN_CHARS_PER_PAGE = 20


class ExtractError(Exception):
    """The file can't be read (corrupt, password-protected, DRM, ...)."""


@dataclass
class Section:
    seq: int  # PDF page index (0-based) or EPUB spine position
    label: str  # printed page label, or chapter title
    text: str
    href: str | None = None  # EPUB: chapter file inside the zip


@dataclass
class Extracted:
    sections: list[Section] = field(default_factory=list)
    units: int = 0  # total pages or chapters, including empty ones
    no_text: bool = False


def normalize_text(text: str) -> str:
    """Clean extracted text so searches match: ligatures, soft hyphens, line-break hyphens, whitespace."""
    text = unicodedata.normalize("NFKC", text)  # "ﬁ" -> "fi", non-breaking space -> space, ...
    text = re.sub("[­​‌‍﻿]", "", text)
    # Rejoin words hyphenated across a line break: "medi-\ncine" -> "medicine",
    # but keep real hyphens before capitals and digits: "Anglo-\nSaxon" -> "Anglo-Saxon".
    text = re.sub(r"(\w)-[ \t]*\r?\n\s*(?=[a-z])", r"\1", text)
    text = re.sub(r"(\w)-[ \t]*\r?\n\s*(?=\w)", r"\1-", text)
    return re.sub(r"\s+", " ", text).strip()


def extract(path: Path, kind: str) -> Extracted:
    if kind == "pdf":
        return extract_pdf(path)
    if kind == "epub":
        return extract_epub(path)
    raise ExtractError(f"unsupported file type: {kind}")


# --- PDF ---------------------------------------------------------------------


def extract_pdf(path: Path) -> Extracted:
    try:
        doc = pymupdf.open(path)
    except Exception as e:
        raise ExtractError(f"can't open PDF: {e}") from None
    with doc:
        if doc.needs_pass:
            raise ExtractError("password protected")
        result = Extracted(units=doc.page_count)
        chars = 0
        for page in doc:
            text = normalize_text(page.get_text("text"))
            chars += len(text)
            if text:
                label = page.get_label() or str(page.number + 1)
                result.sections.append(Section(page.number, label, text))
        result.no_text = chars < MIN_CHARS_PER_PAGE * max(doc.page_count, 1)
    return result


# --- EPUB --------------------------------------------------------------------

_HTML_TYPES = {"application/xhtml+xml", "text/html"}
_BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt", "figcaption",
    "figure", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "nav", "ol",
    "p", "pre", "section", "table", "td", "th", "tr", "ul",
}
_SKIP_TAGS = {"head", "script", "style", "title"}
_HEADING_TAGS = {"h1", "h2", "h3"}


def _local(tag: str) -> str:
    """Element name without its XML namespace."""
    return tag.rsplit("}", 1)[-1]


class _HtmlText(HTMLParser):
    """Collects the visible text of an XHTML chapter, its first heading, and any links."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.heading: str | None = None
        self.links: list[tuple[str, str]] = []  # (href, text)
        self._skip = 0
        self._heading_parts: list[str] | None = None
        self._link: tuple[str, list[str]] | None = None

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip += 1
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")
        if tag in _HEADING_TAGS and self.heading is None and self._heading_parts is None:
            self._heading_parts = []
        if tag == "a":
            href = dict(attrs).get("href")
            self._link = (href, []) if href else None

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS and self._skip:
            self._skip -= 1
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")
        if tag in _HEADING_TAGS and self._heading_parts is not None:
            self.heading = " ".join("".join(self._heading_parts).split()) or None
            self._heading_parts = None
        if tag == "a" and self._link:
            href, text = self._link
            self.links.append((href, " ".join("".join(text).split())))
            self._link = None

    def handle_data(self, data):
        if self._skip:
            return
        self.parts.append(data)
        if self._heading_parts is not None:
            self._heading_parts.append(data)
        if self._link:
            self._link[1].append(data)

    @property
    def text(self) -> str:
        return "".join(self.parts)


class _Epub:
    def __init__(self, zf: zipfile.ZipFile):
        self.zf = zf
        # Some EPUBs reference files with different capitalisation than the zip holds.
        self._names = {n.lower(): n for n in zf.namelist()}

    def read(self, name: str) -> bytes:
        real = self._names.get(name.lower())
        if real is None:
            raise KeyError(name)
        return self.zf.read(real)

    def read_text(self, name: str) -> str:
        data = self.read(name)
        for encoding in ("utf-8-sig", "utf-16"):
            try:
                return data.decode(encoding)
            except UnicodeDecodeError:
                pass
        return data.decode("cp1252", errors="replace")


def _resolve(base: str, href: str) -> str:
    """Resolve an href relative to a file inside the zip, dropping any #fragment."""
    href = unquote(href.split("#", 1)[0])
    return posixpath.normpath(posixpath.join(base, href)) if href else ""


def extract_epub(path: Path) -> Extracted:
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as e:
        raise ExtractError(f"can't open EPUB: {e}") from None
    with zf:
        epub = _Epub(zf)
        try:
            return _extract_epub(epub)
        except (KeyError, ET.ParseError, zipfile.BadZipFile) as e:
            raise ExtractError(f"malformed EPUB: {e}") from None


def _extract_epub(epub: _Epub) -> Extracted:
    _check_drm(epub)

    container = ET.fromstring(epub.read("META-INF/container.xml"))
    rootfile = next((el for el in container.iter() if _local(el.tag) == "rootfile"), None)
    if rootfile is None or not rootfile.get("full-path"):
        raise ExtractError("malformed EPUB: no package file in container.xml")
    opf_path = rootfile.get("full-path")
    opf_dir = posixpath.dirname(opf_path)
    opf = ET.fromstring(epub.read(opf_path))

    manifest = {}
    spine_ids = []
    toc_id = None
    for el in opf.iter():
        name = _local(el.tag)
        if name == "item" and el.get("id") and el.get("href"):
            manifest[el.get("id")] = el
        elif name == "spine":
            toc_id = el.get("toc")
        elif name == "itemref" and el.get("idref"):
            spine_ids.append(el.get("idref"))

    titles = _toc_titles(epub, manifest, toc_id, opf_dir)

    result = Extracted()
    for seq, idref in enumerate(spine_ids):
        item = manifest.get(idref)
        if item is None or item.get("media-type") not in _HTML_TYPES:
            continue
        href = _resolve(opf_dir, item.get("href"))
        result.units += 1
        try:
            html = epub.read_text(href)
        except KeyError:
            continue  # listed in the spine but missing from the zip
        parser = _HtmlText()
        parser.feed(html)
        parser.close()
        text = normalize_text(parser.text)
        if text:
            label = titles.get(href) or parser.heading or f"Section {result.units}"
            result.sections.append(Section(seq, label, text, href))
    result.no_text = not result.sections
    return result


def _check_drm(epub: _Epub) -> None:
    """Refuse EPUBs whose chapters are encrypted. Font obfuscation alone is harmless."""
    try:
        data = epub.read("META-INF/encryption.xml")
    except KeyError:
        return
    for el in ET.fromstring(data).iter():
        if _local(el.tag) == "CipherReference":
            uri = (el.get("URI") or "").lower()
            if uri.endswith((".xhtml", ".html", ".htm", ".xml")):
                raise ExtractError("DRM protected")


def _toc_titles(epub: _Epub, manifest: dict, toc_id: str | None, opf_dir: str) -> dict[str, str]:
    """Map chapter file -> title from the EPUB 3 nav document or EPUB 2 NCX. First entry per file wins."""
    titles: dict[str, str] = {}

    def add(base: str, href: str | None, title: str) -> None:
        if href and title:
            titles.setdefault(_resolve(base, href), title)

    nav = next((it for it in manifest.values() if "nav" in (it.get("properties") or "").split()), None)
    if nav is not None:
        nav_path = _resolve(opf_dir, nav.get("href"))
        try:
            parser = _HtmlText()
            parser.feed(epub.read_text(nav_path))
            parser.close()
            for href, text in parser.links:
                add(posixpath.dirname(nav_path), href, text)
        except KeyError:
            pass

    ncx = manifest.get(toc_id) if toc_id else None
    if ncx is not None:
        ncx_path = _resolve(opf_dir, ncx.get("href"))
        try:
            root = ET.fromstring(epub.read(ncx_path))
        except (KeyError, ET.ParseError):
            return titles
        for point in root.iter():
            if _local(point.tag) != "navPoint":
                continue
            label = src = None
            for child in point:
                if _local(child.tag) == "navLabel":
                    label = " ".join("".join(child.itertext()).split())
                elif _local(child.tag) == "content":
                    src = child.get("src")
            add(posixpath.dirname(ncx_path), src, label or "")
    return titles
