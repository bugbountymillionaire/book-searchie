"""Take the README screenshots from the demo library.

    pip install playwright
    python tools/demo_library.py E:/BookSearchieDemo
    python tools/screenshots.py E:/BookSearchieDemo --browser msedge

Starts Book-Searchie on the demo library (port 8005), photographs four pages at 1280x800
in light mode into docs/screenshots/, and stops the server again.

--browser picks an installed browser: msedge or chrome. Without it, Playwright's own
Chromium is used, which needs a one-off `playwright install chromium`.

The "Can't search" screenshot shows where the demo's OCR copies would go, which is inside
the demo folder. So build the demo in a short, neutral place like E:/BookSearchieDemo,
not under your user folder, or your username will appear in the screenshot.
"""

from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from book_searchie.config import load_config  # noqa: E402
from book_searchie.highlight import query_terms  # noqa: E402
from book_searchie.web import render  # noqa: E402

BASE = "http://localhost:8005"
OUT = ROOT / "docs" / "screenshots"


def best_pdf_page(path: Path, query: str) -> int:
    """The page with the most matches, counted as the viewer does, among pages with a picture."""
    terms = query_terms(query)
    with pymupdf.open(path) as doc:
        pictured = [page.number for page in doc if page.get_images()]
    return max(pictured, key=lambda n: (len(render.pdf_page(path, n, terms).matches), -n))


def best_section(db: sqlite3.Connection, file_id: int, word: str) -> int:
    """The chapter with the most matches (the first, if several tie)."""
    rows = db.execute("SELECT seq, lower(text) FROM sections WHERE file_id = ?", (file_id,))
    seq, _ = max(rows, key=lambda row: (row[1].count(word), -row[0]))
    return seq


def shots(config_path: Path) -> list[tuple[str, str, int]]:
    """(file name, page to open, how many times to press "n" for the next match)."""
    config = load_config(config_path)
    db = sqlite3.connect(config.index_path)
    ids = {title: (fid, Path(path)) for fid, title, path in db.execute("SELECT id, title, path FROM files")}
    joints, joints_path = ids["Fairham - Woodwork Joints"]
    herbal, _ = ids["Culpeper - The Complete Herbal"]
    return [
        ("search", "/?q=honey", 0),
        # The second match, so the current-match outline is on a highlighted word mid-page.
        ("pdf-viewer", f"/view/{joints}/{best_pdf_page(joints_path, 'dovetail')}?q=dovetail", 1),
        ("epub-viewer", f"/view/{herbal}/{best_section(db, herbal, 'comfrey')}?q=comfrey", 0),
        ("cant-search", "/problems", 0),
    ]


def wait_for_server(server: subprocess.Popen) -> None:
    for _ in range(60):
        if server.poll() is not None:
            raise SystemExit("The demo server stopped. Is port 8005 already in use?")
        try:
            urllib.request.urlopen(BASE, timeout=2)
            return
        except OSError:
            time.sleep(0.5)
    raise SystemExit("The demo server didn't start.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("demo", type=Path, help="the folder tools/demo_library.py built")
    parser.add_argument("--browser", choices=["msedge", "chrome"], help="use an installed browser")
    args = parser.parse_args()
    demo = args.demo.resolve()
    if demo.is_relative_to(Path.home()):
        raise SystemExit(f"{demo} is inside your user folder, which would show in a screenshot. "
                         "Build the demo somewhere neutral, like E:/BookSearchieDemo.")
    config_path = demo / "book-searchie.toml"
    if not config_path.is_file():
        raise SystemExit(f"No demo library in {demo}. Run tools/demo_library.py first.")

    from playwright.sync_api import sync_playwright  # only needed here

    plan = shots(config_path)
    OUT.mkdir(parents=True, exist_ok=True)
    server = subprocess.Popen(
        [sys.executable, "-m", "book_searchie", "--config", str(config_path), "serve", "--no-browser"],
        cwd=demo, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        wait_for_server(server)
        with sync_playwright() as p:
            browser = p.chromium.launch(channel=args.browser)
            page = browser.new_page(viewport={"width": 1280, "height": 800}, color_scheme="light")
            for name, url, steps in plan:
                page.goto(BASE + url)
                page.wait_for_load_state("load")
                page.wait_for_timeout(500)
                for _ in range(steps):
                    page.keyboard.press("n")
                    page.wait_for_timeout(900)  # let the smooth scroll finish
                page.mouse.move(0, 799)  # nothing hovered
                page.screenshot(path=OUT / f"{name}.png")
                print(f"{OUT / name}.png")
            browser.close()
    finally:
        server.terminate()
        server.wait(timeout=10)


if __name__ == "__main__":
    main()
