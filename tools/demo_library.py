"""Build a small demo library of public-domain books from Project Gutenberg.

    python tools/demo_library.py E:/BookSearchieDemo

Downloads seven EPUBs (about 35 MB, kept in DEST/downloads so later runs are quick), then
builds DEST/library with four topics, writes DEST/book-searchie.toml (port 8005, so it
doesn't clash with your own library on 8004) and indexes it. The library has EPUBs, text
PDFs (laid out from EPUBs) and one image-only "scanned" PDF, so every page of the web
interface has something to show. Try it with:

    cd E:/BookSearchieDemo
    book-searchie serve

All the books are public domain in the US. They're downloaded, not stored in this repo.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
import urllib.request
from pathlib import Path

import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from book_searchie.config import load_config  # noqa: E402
from book_searchie.db import Database  # noqa: E402
from book_searchie.indexer import run_index  # noqa: E402

GUTENBERG = "https://www.gutenberg.org/ebooks/{id}.epub3.images"

# Gutenberg ebook number -> where it goes, and as what.
EPUBS = {
    49513: "Herbalism/Culpeper - The Complete Herbal.epub",
    20763: "Woodwork/Zerbe - Carpentry for Boys.epub",
    16232: "Gardening/Sutton - The Culture of Vegetables and Flowers.epub",
    24583: "Beekeeping/Langstroth - On the Hive and the Honey-Bee.epub",
}
PDFS = {
    21531: "Woodwork/Fairham - Woodwork Joints.pdf",
    9550: "Gardening/Bailey - Manual of Gardening.pdf",
}
# Pages as pictures only, no text layer, so it shows up on the "Can't search" page.
SCANNED = {25185: ("Beekeeping/Quinby - Mysteries of Bee-keeping Explained.pdf", 12)}

CONFIG = """\
[library]
roots = ["library"]

[index]
path = "book-searchie.db"

[server]
port = 8005
"""


def download(book_id: int, downloads: Path) -> Path:
    path = downloads / f"{book_id}.epub"
    if not path.exists():
        print(f"Downloading Gutenberg #{book_id}...", flush=True)
        request = urllib.request.Request(GUTENBERG.format(id=book_id), headers={"User-Agent": "book-searchie demo"})
        with urllib.request.urlopen(request, timeout=120) as response:
            data = response.read()
        if not data.startswith(b"PK"):
            raise SystemExit(f"Gutenberg #{book_id} didn't download as an EPUB; try again later.")
        path.write_bytes(data)
        time.sleep(1)  # be polite to Gutenberg's servers
    return path


def epub_to_pdf(epub: Path) -> pymupdf.Document:
    doc = pymupdf.open(epub)
    doc.layout(width=432, height=648, fontsize=11)  # 6 x 9 inch pages, like a printed book
    return pymupdf.open("pdf", doc.convert_to_pdf())


def build(dest: Path) -> None:
    downloads = dest / "downloads"
    library = dest / "library"
    downloads.mkdir(parents=True, exist_ok=True)
    if library.exists():
        shutil.rmtree(library)

    for book_id, rel in EPUBS.items():
        (library / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(download(book_id, downloads), library / rel)
    for book_id, rel in PDFS.items():
        (library / rel).parent.mkdir(parents=True, exist_ok=True)
        epub_to_pdf(download(book_id, downloads)).save(library / rel, garbage=3, deflate=True)
    for book_id, (rel, pages) in SCANNED.items():
        (library / rel).parent.mkdir(parents=True, exist_ok=True)
        text_pdf = epub_to_pdf(download(book_id, downloads))
        scan = pymupdf.open()
        for page in list(text_pdf)[:pages]:
            pix = page.get_pixmap(dpi=110, colorspace=pymupdf.csGRAY)
            out = scan.new_page(width=page.rect.width, height=page.rect.height)
            out.insert_image(out.rect, stream=pix.tobytes("jpeg", jpg_quality=70))
        scan.save(library / rel, garbage=3, deflate=True)

    config_path = dest / "book-searchie.toml"
    config_path.write_text(CONFIG, encoding="utf-8")
    config = load_config(config_path)
    config.index_path.unlink(missing_ok=True)  # start fresh, as the books were rebuilt
    with Database.open(config.index_path) as db:
        run_index(config, db, progress=lambda msg: print(f"Indexing {msg}", flush=True))
        books = db.stats()["books"]
    print(f"Demo library ready: {books} books in {library}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("dest", type=Path, help="folder to build the demo in (created if needed)")
    build(parser.parse_args().dest.resolve())


if __name__ == "__main__":
    main()
