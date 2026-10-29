from __future__ import annotations

import zipfile
from pathlib import Path

import pymupdf
import pytest

from book_searchie.config import Config
from book_searchie.db import Database


def make_pdf(path: Path, pages: list[str], labels: list[dict] | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        if text:
            page.insert_textbox(pymupdf.Rect(50, 50, 550, 800), text, fontsize=11)
    if labels:
        doc.set_page_labels(labels)
    doc.save(path)
    doc.close()
    return path


def make_epub(path: Path, chapters: list[tuple[str, str]], *, nav: bool = True, extra: dict | None = None) -> Path:
    """Build a minimal EPUB 3. chapters is a list of (title, body html)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    manifest, spine, links = [], [], []
    files = {}
    for i, (title, body) in enumerate(chapters, 1):
        name = f"text/ch{i}.xhtml"
        files[f"OEBPS/{name}"] = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>ignored</title>'
            f"<style>p {{ color: red }}</style></head><body><h1>{title}</h1>{body}</body></html>"
        )
        manifest.append(f'<item id="ch{i}" href="{name}" media-type="application/xhtml+xml"/>')
        spine.append(f'<itemref idref="ch{i}"/>')
        links.append(f'<li><a href="{name}#start">TOC {title}</a></li>')
    if nav:
        files["OEBPS/nav.xhtml"] = (
            '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">'
            f'<body><nav epub:type="toc"><ol>{"".join(links)}</ol></nav></body></html>'
        )
        manifest.append('<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>')
    files["OEBPS/content.opf"] = (
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
        f'<manifest>{"".join(manifest)}</manifest><spine>{"".join(spine)}</spine></package>'
    )
    files["META-INF/container.xml"] = (
        '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">'
        '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
        "</rootfiles></container>"
    )
    files.update(extra or {})
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        for name, data in files.items():
            zf.writestr(name, data)
    return path


@pytest.fixture
def library(tmp_path: Path) -> Path:
    root = tmp_path / "Knowledge"
    make_pdf(
        root / "Herbalism" / "Culpeper.pdf",
        ["Introduction to the herbal.", "The root of black cohosh is bitter.", "Echinacea purpurea grows tall."],
    )
    make_epub(
        root / "Herbalism" / "Modern Herbal.epub",
        [("Roots", "<p>Valerian root helps sleep.</p>"), ("Leaves", "<p>Nettle leaves sting.</p>")],
    )
    make_pdf(root / "Woodwork" / "Joinery.pdf", ["Dovetail joints are strong."])
    make_pdf(root / "Gardening" / "Seasonal" / "Spring.pdf", ["Sow broad beans in spring."])
    make_pdf(root / "Woodwork" / "_unsorted" / "Hidden.pdf", ["Secret dovetail notes."])
    make_pdf(root / "_inbox" / "New.pdf", ["Inbox echinacea notes."])
    make_pdf(root / "Loose.pdf", ["A loose book about compost."])
    return root


@pytest.fixture
def config(library: Path, tmp_path: Path) -> Config:
    return Config(roots=[library], index_path=tmp_path / "index.db")


@pytest.fixture
def db(config: Config):
    with Database.open(config.index_path) as database:
        yield database
