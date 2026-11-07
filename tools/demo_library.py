"""Build a small demo library of public-domain books from Project Gutenberg.

    python tools/demo_library.py E:/BookSearchieDemo

Downloads seven EPUBs (about 35 MB, kept in DEST/downloads so later runs are quick), then
builds DEST/library with four topics, writes DEST/book-searchie.toml (port 8005, so it
doesn't clash with your own library on 8004) and indexes it. The library has EPUBs, text
PDFs (laid out from EPUBs) and one image-only "scanned" PDF, so every page of the web
interface has something to show. It also adds a few sample KB entries (Nettle, Comfrey...)
with text from the books, your own text and text from other sources under sub-headings,
plus tags, other names, and books marked as researched for a search. Try it with:

    cd E:/BookSearchieDemo
    book-searchie serve

All the books are public domain in the US. They're downloaded, not stored in this repo.
"""

from __future__ import annotations

import argparse
import re
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
from book_searchie.entries import Piece  # noqa: E402
from book_searchie.notes import NotesStore  # noqa: E402

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

[notes]
path = "book-searchie-notes.db"

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


# Sample KB entries. Text from a book is the sentence that starts with the given words,
# found in the downloaded text, so it points at the right page or chapter. Lines starting
# with # are sub-headings, as when typing in Save to KB; "Uses" and "Timing" aren't on the
# starting list, so they're added to it.
BOOK_TEXT = [
    ("Nettle", "Culpeper - The Complete Herbal", "# Poultice", "An handful of the leaves of green Nettles"),
    ("Comfrey", "Culpeper - The Complete Herbal", "# Uses", "The water of Comfrey solders broken bones"),
    ("Swarms", "Langstroth - On the Hive and the Honey-Bee", "# Timing",
     "If the Apiarian wishes, to be sure the first season"),
    ("Dovetail halving", "Fairham - Woodwork Joints", "", "This is a strong joint, used where outside strain"),
]
# (entry, topic, text, where it's from: None for your own words, or (name, link)).
OTHER_TEXT = [
    ("Nettle", "Herbalism", "Grows by the compost heap, in full sun.\n"
     "# Harvesting\nPick the top four leaves in spring, before it flowers.\n"
     "# Cautions\nWear gloves to pick it. Drying or cooking takes the sting away.", None),
    ("Nettle", "Herbalism", "# Tea\nA handful of fresh tops in a pot, steeped for ten minutes.",
     ("Spring herb walk with a local forager", "")),
    ("Comfrey", "Gardening", "# Harvesting\nCut the leaves three or four times a summer for:\n\n"
     "- compost\n- a **liquid feed** for the tomatoes", None),
    ("Comfrey", "Herbalism", "# Cautions\nFor use on the skin only.", None),
    ("Swarms", "Beekeeping", "# Timing\nTry two swarms in one hive next spring.", None),
    ("Hive inspection", "Beekeeping", "# Checklist\n- Queen or eggs seen\n- Brood pattern\n- Stores\n"
     "- Space for the colony to grow", None),
    ("Chisels", "Woodwork", "# Sharpening\nFlatten the back first. A honing guide keeps the angle steady.",
     ("Evening class at the community workshop", "")),
    ("Culpeper's Herbal", "Herbalism", "Free to read online, in several formats.",
     ("Project Gutenberg", "https://www.gutenberg.org/ebooks/49513")),
]
TAGS = {
    "Nettle": ["Joint pain", "Spring greens"], "Comfrey": ["Wounds", "Compost"], "Swarms": ["Honey"],
    "Hive inspection": ["Spring jobs"], "Dovetail halving": ["Joints"], "Chisels": ["Sharpening"],
}
OTHER_NAMES = {"Nettle": ["Urtica dioica", "Stinging nettle"], "Comfrey": ["Knitbone"]}
# Books marked Research complete: (search, book).
RESEARCHED = [("honey", "Langstroth - On the Hive and the Honey-Bee"), ("nettle", "Culpeper - The Complete Herbal")]


def _find_quote(db: Database, file_id: int, start: str) -> tuple[int, str, str | None, str]:
    """(seq, label, href, sentence) for the sentence starting with `start` in a book."""
    for seq, label, href, text in db.conn.execute(
            "SELECT seq, label, href, text FROM sections WHERE file_id = ? ORDER BY seq", (file_id,)):
        flat = " ".join(text.split())
        at = flat.find(start)
        if at >= 0:
            stop = re.search(r"[.!?](\s|$)", flat[at:])
            return seq, label, href, flat[at:at + stop.end()].strip() if stop else flat[at:]
    raise SystemExit(f"Couldn't find {start!r} in the demo books. Has the Gutenberg text changed?")


def add_sample_entries(config, db: Database) -> int:
    books = {row["title"]: row for row in db.conn.execute("SELECT id, title, path, topic FROM files")}
    config.notes_path.unlink(missing_ok=True)  # the demo starts fresh each time
    with NotesStore.open(config.notes_path, config.headings) as store:
        for entry, book, heading, start in BOOK_TEXT:
            row = books[book]
            seq, label, href, quote = _find_quote(db, row["id"], start)
            source = Piece(body="x", path=row["path"], seq=seq, label=label, href=href,
                           book_title=row["title"], topic=row["topic"])
            store.add_text(entry, f"{heading}\n{quote}" if heading else quote, source)
        for entry, topic, text, where in OTHER_TEXT:
            source = Piece(body="x", topic=topic, source_ref=where[0], source_url=where[1] or None) if where \
                else Piece(body="x", topic=topic)
            store.add_text(entry, text, source)
        for entry, tags in TAGS.items():
            store.set_entry_tags(store.find_entry(entry).id, tags)
        for entry, names in OTHER_NAMES.items():
            for name in names:
                store.add_entry_alias(store.find_entry(entry).id, name)
        store.add_alias("tag", "Joint pain", "Arthritis")  # an old name: searching for it finds Joint pain
        for term, book in RESEARCHED:
            row = books[book]
            store.set_researched(term, row["path"], True, book_title=row["title"], topic=row["topic"])
        return store.entry_count()


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
        entries = add_sample_entries(config, db)
    print(f"Demo library ready: {books} books and {entries} KB entries in {dest}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("dest", type=Path, help="folder to build the demo in (created if needed)")
    build(parser.parse_args().dest.resolve())


if __name__ == "__main__":
    main()
