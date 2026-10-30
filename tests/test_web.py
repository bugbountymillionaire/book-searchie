import pymupdf
import pytest
from fastapi.testclient import TestClient

from book_searchie import ocr
from book_searchie.highlight import query_terms, text_spans
from book_searchie.indexer import run_index
from book_searchie.web.app import create_app
from book_searchie.web.render import render_epub_chapter

from .conftest import make_epub, make_pdf

H = {"X-Book-Searchie": "1"}


@pytest.fixture
def client(config, db, library):
    make_epub(
        library / "Herbalism" / "Tricky.epub",
        [("Danger", '<p onclick="evil()">Nettle <script>alert(1)</script><a href="javascript:x">soup</a></p>'
                    '<img src="../images/pic.png" alt="a &quot;pic&quot;"><iframe src="http://x"></iframe>')],
    )
    run_index(config, db)
    return TestClient(create_app(config, allowed_hosts=["testserver"]))


def _file_id(db, title):
    return db.conn.execute("SELECT id FROM files WHERE title = ?", (title,)).fetchone()[0]


def test_query_terms():
    terms = query_terms('"Black Cohosh" root* AND NOT valerian OR (x NOT (y z))')
    assert terms == [(("black", "cohosh"), False), (("root",), True), (("x",), False)]
    assert text_spans("The black cohosh roots; Rooted.", terms) == [(4, 16), (17, 22), (24, 30)]


def test_home_and_search(client):
    page = client.get("/")
    assert page.status_code == 200 and "Search 6 books" in page.text
    res = client.get("/", params={"q": '"black cohosh"'})
    assert "Culpeper" in res.text and "<mark>black cohosh</mark>" in res.text
    assert "Page 2" in res.text


def test_search_groups_by_book_and_filters_topic(client):
    res = client.get("/", params={"q": "root OR dovetail"})
    assert res.text.count('<article class="book">') == 3
    res = client.get("/", params={"q": "root OR dovetail", "topic": "Woodwork"})
    assert res.text.count('<article class="book">') == 1


def test_bad_query_shows_message(client):
    res = client.get("/", params={"q": "root AND"})
    assert res.status_code == 200 and "understand that search" in res.text


def test_snippets_are_escaped(client, config, db, library):
    make_pdf(library / "Herbalism" / "Html.pdf", ["Tincture <b>bold</b> & more"])
    run_index(config, db)
    res = client.get("/", params={"q": "tincture"})
    assert "&lt;b&gt;" in res.text and "<b>bold</b>" not in res.text


def test_pdf_viewer_and_image(client, db):
    fid = _file_id(db, "Culpeper")
    res = client.get(f"/view/{fid}/1", params={"q": "cohosh OR echinacea"})
    assert res.status_code == 200
    assert "Page with match 1 of 2" in res.text and f"/view/{fid}/2?q=" in res.text
    img = client.get(f"/page/{fid}/1.png", params={"q": "cohosh"})
    assert img.status_code == 200 and img.headers["content-type"] == "image/png"
    assert client.get(f"/page/{fid}/99.png").status_code == 404


def test_viewer_bars_stay_on_screen(client, db):
    # Everything above the page itself sits in the sticky header, for PDFs and EPUBs alike.
    for name, seq in (("Culpeper", 1), ("Tricky", 0)):
        res = client.get(f"/view/{_file_id(db, name)}/{seq}", params={"q": "nettle"})
        head, body = res.text.split("<main", 1)
        assert 'class="sticky-head"' in head and 'class="viewer-bar"' in head and 'class="viewer-tools"' in head
        assert "viewer-bar" not in body and "viewer-tools" not in body


def test_pdf_highlight_is_drawn(client, db):
    fid = _file_id(db, "Culpeper")
    plain = client.get(f"/page/{fid}/1.png").content
    marked = client.get(f"/page/{fid}/1.png", params={"q": "cohosh"}).content
    assert plain != marked


def test_epub_viewer_is_sanitised(client, db):
    fid = _file_id(db, "Tricky")
    res = client.get(f"/view/{fid}/0", params={"q": "nettle"})
    assert res.status_code == 200
    chapter = res.text.split('id="chapter"', 1)[1].split('</article>', 1)[0]
    assert "<script" not in chapter and "onclick" not in chapter and "javascript:" not in chapter
    assert "<iframe" not in chapter and "alert(1)" not in chapter
    assert '<mark id="m0">Nettle</mark>' in chapter
    assert f'src="/asset/{fid}?name=OEBPS/images/pic.png"' in chapter
    assert 'alt="a &#34;pic&#34;"' in chapter or 'alt="a &quot;pic&quot;"' in chapter


def test_epub_sanitiser_balances_tags():
    terms = query_terms("x")
    html, marks = _sanitise("<div><p>x <b>y</p><p/>z</div></i><style>p{}</style>", terms)
    assert html == "<div><p><mark id=\"m0\">x</mark> <b>y</b></p><p></p>z</div>" and marks == 1


def _sanitise(source, terms):
    from book_searchie.web.render import _Sanitizer

    s = _Sanitizer(terms, lambda n: n, "")
    s.feed(source)
    s.close()
    return s.result(), s.marks


def test_epub_asset_only_serves_images(client, db, library):
    fid = _file_id(db, "Tricky")
    assert client.get(f"/asset/{fid}", params={"name": "OEBPS/content.opf"}).status_code == 404
    assert client.get(f"/asset/{fid}", params={"name": "../../etc/passwd.png"}).status_code == 404


def test_unknown_book_and_host(client, config):
    assert client.get("/view/9999/0").status_code == 404
    other = TestClient(create_app(config))  # only localhost allowed
    assert other.get("/").status_code == 400


def test_book_outside_library_is_refused(client, config, db, tmp_path):
    fid = _file_id(db, "Joinery")
    outside = make_pdf(tmp_path / "outside.pdf", ["private"])
    db.conn.execute("UPDATE files SET path = ? WHERE id = ?", (str(outside), fid))
    db.conn.commit()
    assert client.get(f"/page/{fid}/0.png").status_code == 404


def test_problems_page(client, config, db, library):
    make_pdf(library / "Herbalism" / "Scanned.pdf", ["", ""])
    run_index(config, db)
    res = client.get("/problems")
    assert res.status_code == 200
    assert "Scanned books" in res.text and "Herbalism/Scanned.pdf" in res.text
    assert '<span class="badge">1</span>' in res.text


def test_ocr_api_needs_header(client):
    assert client.post("/api/ocr", json={}).status_code == 403
    assert client.post("/api/ocr/cancel", json={}).status_code == 403


def test_ocr_api_validates(client, config, db, library):
    assert client.post("/api/ocr", json={}, headers=H).status_code == 400  # nothing scanned
    make_pdf(library / "Herbalism" / "Scanned.pdf", ["", ""])
    run_index(config, db)
    res = client.post("/api/ocr", json={"language": "eng; rm -rf /"}, headers=H)
    assert res.status_code == 400


def test_ocr_api_runs_job(client, config, db, library, monkeypatch):
    make_pdf(library / "Herbalism" / "Scanned.pdf", ["", ""])
    run_index(config, db)
    started = {}

    def fake_start(self, rels, language="eng"):
        started.update(rels=rels, language=language)

    monkeypatch.setattr("book_searchie.web.jobs.OcrJob.start", fake_start)
    res = client.post("/api/ocr", json={"ids": None, "language": "eng+fra"}, headers=H)
    assert res.status_code == 200
    assert started == {"rels": ["Herbalism/Scanned.pdf"], "language": "eng+fra"}


def test_ocr_job_end_to_end(config, db, library, monkeypatch):
    """Run the real OcrJob, whose subprocess uses a stand-in for OCRmyPDF."""
    import sys
    import time

    from book_searchie.web.jobs import OcrJob

    make_pdf(library / "Herbalism" / "Scanned.pdf", ["", ""])
    run_index(config, db)
    cfg = library.parent / "book-searchie.toml"
    cfg.write_text(
        f'[library]\nroots = ["{library.as_posix()}"]\n[index]\npath = "{config.index_path.as_posix()}"\n',
        encoding="utf-8",
    )
    config.source = cfg
    # The child process imports this module and swaps in a fake ocr_pdf before running the CLI.
    shim = library.parent / "shim.py"
    shim.write_text(
        "import sys, runpy, pymupdf\n"
        "from book_searchie import ocr\n"
        "def fake(src, dest, language, **kw):\n"
        "    cb = kw.get('on_pages')\n"
        "    dest.parent.mkdir(parents=True, exist_ok=True)\n"
        "    doc = pymupdf.open(); p = doc.new_page(); p.insert_text((50, 50), 'Hawthorn berries ' * 5); doc.save(dest)\n"
        "    if cb: cb('OCR', 1, 1)\n"
        "ocr.ocr_pdf = fake\n"
        "ocr.check_available = lambda: None\n"
        "sys.argv = ['book-searchie'] + sys.argv[1:]\n"
        "from book_searchie.cli import main\n"
        "sys.exit(main())\n",
        encoding="utf-8",
    )
    real_popen = __import__("subprocess").Popen

    def popen(cmd, **kw):
        assert cmd[:3] == [sys.executable, "-m", "book_searchie"]
        return real_popen([sys.executable, str(shim), *cmd[3:]], **kw)

    monkeypatch.setattr("book_searchie.web.jobs.subprocess.Popen", popen)
    job = OcrJob(config)
    job.start(["Herbalism/Scanned.pdf"])
    for _ in range(200):
        if not job.status()["running"]:
            break
        time.sleep(0.1)
    s = job.status()
    assert s["finished"] and s["done"] == ["Herbalism/Scanned.pdf"] and not s["failed"], repr(s)
    assert db.conn.execute("SELECT ocr FROM files WHERE rel = 'Herbalism/Scanned.pdf'").fetchone()[0] == 1
