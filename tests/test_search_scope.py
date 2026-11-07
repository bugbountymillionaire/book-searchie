"""Searching books, the KB and researched books, with the checkboxes in the search box."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from book_searchie.db import Database
from book_searchie.indexer import run_index
from book_searchie.entries import Piece
from book_searchie.notes import NotesStore
from book_searchie.web.app import create_app, scope_params, search_scope


@pytest.fixture
def client(config, db):
    run_index(config, db)
    return TestClient(create_app(config, allowed_hosts=["testserver"]))


@pytest.fixture
def notes(config, db):
    culpeper = db.conn.execute("SELECT path FROM files WHERE title = 'Culpeper'").fetchone()[0]
    with NotesStore.open(config.notes_path) as store:
        store.add_text("Black cohosh", "The root is bitter.",
                       Piece(body="x", path=culpeper, seq=1, label="2", book_title="Culpeper", topic="Herbalism"))
        store.add_text("Tinctures", "# Tincture\nBlack cohosh <b>tincture</b> dose.",
                       Piece(body="x", source_ref="Herb school", topic="Herbalism"))
        store.add_text("Oak", "Cohosh-stained oak: an odd idea.", Piece(body="x", topic="Woodwork"))
        store.set_entry_tags(store.find_entry("Black cohosh").id, ["Hot flushes"])
        yield store


def articles(page, cls):
    return page.count(f'<article class="{cls}')


def researched(client, db, title, q):
    fid = db.conn.execute("SELECT id FROM files WHERE title = ?", (title,)).fetchone()[0]
    client.put("/api/research", json={"file_id": fid, "q": q}, headers={"X-Book-Searchie": "1"})


def test_search_scope_rules():
    # Old links and bookmarks, without scoped=1: researched books go along with Books.
    assert search_scope(None, None) == (True, True, True)
    assert search_scope("1", "1") == (True, True, True)
    assert search_scope("1", None) == (True, False, True)
    assert search_scope(None, "1") == (False, True, False)
    assert search_scope("0", "") == (True, True, True)  # something is always searched
    # From the search box (scoped=1), each box counts.
    assert search_scope("1", "1", None, "1") == (True, True, False)
    assert search_scope(None, None, "1", "1") == (False, False, True)
    assert search_scope(None, None, None, "1") == (True, True, True)
    assert scope_params(True, True, True) == {"books": "1", "kb": "1"}
    assert scope_params(True, False, False) == {"books": "1", "scoped": "1"}
    assert scope_params(False, True, True) == {"kb": "1", "done": "1", "scoped": "1"}
    for b in (True, False):
        for k in (True, False):
            for d in (True, False):
                if b or k or d:
                    p = scope_params(b, k, d)
                    assert search_scope(p.get("books"), p.get("kb"), p.get("done"), p.get("scoped")) == (b, k, d)


def test_checkboxes_in_the_search_box(client):
    page = client.get("/").text
    assert '<input type="checkbox" name="books" value="1" checked> Books' in page
    assert '<input type="checkbox" name="kb" value="1" checked> KB' in page
    assert '<input type="checkbox" name="done" value="1" checked> Researched books' in page
    assert '<input type="hidden" name="scoped" value="1">' in page
    assert "/static/scope.js" in page
    page = client.get("/", params={"q": "cohosh", "kb": "1", "scoped": "1"}).text
    assert 'name="books" value="1" >' in page and 'name="kb" value="1" checked>' in page  # shows what was searched
    assert 'name="done" value="1" >' in page


def test_both_shows_entries_above_books(client, notes):
    page = client.get("/", params={"q": "cohosh"}).text
    assert 'Your KB <span class="dim">(3)</span>' in page
    assert articles(page, "entry-hit") == 3 and articles(page, "book") == 1
    assert page.index("Your KB") < page.index('<article class="book')
    assert page.index(">Black cohosh</a>") < page.index(">Tinctures</a>")  # a title match first
    assert "All 3 matching entries" not in page  # all of them are already shown
    assert "Black <mark>cohosh</mark> &lt;b&gt;tincture&lt;/b&gt; dose." in page  # snippets are escaped
    tinctures = notes.find_entry("Tinctures").id
    assert f'href="/kb/entry/{tinctures}?q=cohosh#h-tincture">Tincture</a>' in page
    assert '<a class="tag" href="/kb?tag=Hot+flushes">Hot flushes</a>' in page


def test_both_links_to_all_entries_when_there_are_more(client, notes):
    for n in range(3):
        notes.add_text(f"More cohosh {n}", "x")
    page = client.get("/", params={"q": "cohosh"}).text
    assert articles(page, "entry-hit") == 3
    assert 'href="/?q=cohosh&amp;topic=&amp;kb=1">All 6 matching entries' in page


def test_kb_only(client, notes):
    page = client.get("/", params={"q": "cohosh", "kb": "1"}).text
    assert "3 matching entries in your KB" in page
    assert articles(page, "entry-hit") == 3 and articles(page, "book") == 0


def test_kb_only_pages(client, notes, monkeypatch):
    from book_searchie.web import app as app_module

    monkeypatch.setattr(app_module, "KB_PER_PAGE", 2)
    page = client.get("/", params={"q": "cohosh", "kb": "1"}).text
    assert articles(page, "entry-hit") == 2 and "Page 1 of 2" in page
    assert 'href="/?q=cohosh&amp;topic=&amp;kb=1&amp;page_no=2"' in page
    assert articles(client.get("/", params={"q": "cohosh", "kb": "1", "page_no": 2}).text, "entry-hit") == 1
    assert articles(client.get("/", params={"q": "cohosh", "kb": "1", "page_no": 9}).text, "entry-hit") == 1


def test_books_only(client, notes):
    page = client.get("/", params={"q": "cohosh", "books": "1"}).text
    assert "Your KB" not in page and articles(page, "entry-hit") == 0 and articles(page, "book") == 1


def test_topic_filter_applies_to_entries(client, notes):
    page = client.get("/", params={"q": "cohosh", "topic": "Woodwork"}).text
    assert 'Your KB <span class="dim">(1)</span>' in page and ">Oak</a>" in page
    assert "No results for <strong>cohosh</strong> in your books in Woodwork" in page
    page = client.get("/", params={"q": "cohosh", "topic": "Gardening", "kb": "1"}).text
    assert "Nothing in your KB matches" in page
    assert 'href="/?q=cohosh&amp;kb=1">Search all topics' in page


def test_no_notes_file_yet(client, config):
    page = client.get("/", params={"q": "cohosh"}).text
    assert "Your KB" not in page and articles(page, "book") == 1
    assert "Nothing in your KB matches" in client.get("/", params={"q": "cohosh", "kb": "1"}).text
    assert not config.notes_path.exists()


def test_bad_query_in_kb_only(client, notes):
    page = client.get("/", params={"q": "cohosh AND", "kb": "1"}).text
    assert "understand that search" in page


def test_home_counts_entries(client, notes):
    assert "Search 5 books and 3 KB entries" in client.get("/").text


def test_researched_books_in_their_own_section(client, db):
    researched(client, db, "Culpeper", "root")
    page = client.get("/", params={"q": "root"}).text
    assert "1 of 2 books researched for “root”" in page
    assert page.index('<article class="book">') < page.index("Researched for “root”</h2>") \
        < page.index('<article class="book researched">')
    assert page.index("Modern Herbal</a>") < page.index("Culpeper</a>")  # not yet researched first


def test_leaving_out_researched_books(client, db):
    researched(client, db, "Culpeper", "root")
    page = client.get("/", params={"q": "root", "books": "1", "kb": "1", "scoped": "1"}).text
    assert articles(page, "book") == 1 and "Modern Herbal</a>" in page and "Culpeper</a>" not in page
    assert "1 researched book not shown" in page
    assert 'href="/?q=root&amp;topic=&amp;books=1&amp;kb=1">Show it' in page

    only_done = client.get("/", params={"q": "root", "done": "1", "scoped": "1"}).text
    assert articles(only_done, "book") == 1 and "Culpeper</a>" in only_done and "Modern Herbal</a>" not in only_done
    assert "Researched for “root”</h2>" in only_done

    researched(client, db, "Modern Herbal", "root")
    page = client.get("/", params={"q": "root", "books": "1", "scoped": "1"}).text
    assert "All 2 books with <strong>root</strong> in are researched for “root”" in page
    page = client.get("/", params={"q": "valerian", "done": "1", "scoped": "1"}).text
    assert "You haven't marked any of the 1 book with <strong>valerian</strong> as researched yet." in page


def test_history_records_the_scope(client, db, notes):
    client.get("/", params={"q": "cohosh"})
    client.get("/", params={"q": "cohosh", "kb": "1"})
    client.get("/", params={"q": "cohosh", "books": "1"})
    rows = [(r["query"], r["scope"], r["hits"], r["notes"]) for r in db.recent_searches()]
    assert rows == [("cohosh", "books", 1, None), ("cohosh", "kb", 0, 3), ("cohosh", "both", 1, 3)]

    home = client.get("/").text
    assert 'href="/?q=cohosh&amp;topic=&amp;books=1">cohosh</a>' in home
    assert 'href="/?q=cohosh&amp;topic=&amp;kb=1">cohosh</a>' in home
    assert 'href="/?q=cohosh&amp;topic=&amp;books=1&amp;kb=1">cohosh</a>' in home
    assert "1 page, 3 entries" in home and ">KB</span>" in home and ">Books</span>" in home


def test_old_history_is_kept_when_scope_is_added(config):
    # The history table as it was before the KB.
    config.index_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.index_path)
    conn.executescript(
        "CREATE TABLE searches ("
        " id INTEGER PRIMARY KEY,"
        " query TEXT NOT NULL COLLATE NOCASE,"
        " topic TEXT NOT NULL DEFAULT '' COLLATE NOCASE,"
        " hits INTEGER NOT NULL,"
        " searched_at TEXT NOT NULL,"
        " UNIQUE (query, topic));"
        "INSERT INTO searches (query, topic, hits, searched_at) VALUES"
        " ('nettle', '', 12, '2026-11-01T10:00:00+00:00'),"
        " ('comfrey', 'Herbalism', 0, '2026-11-02T10:00:00+00:00');"
    )
    conn.close()
    with Database.open(config.index_path) as db:
        rows = [(r["query"], r["topic"], r["scope"], r["hits"], r["notes"]) for r in db.recent_searches()]
        assert rows == [("comfrey", "Herbalism", "books", 0, None), ("nettle", "", "books", 12, None)]
        db.record_search("nettle", "", 3, "both", notes=1)  # a different scope is a separate entry
        assert len(db.recent_searches()) == 3
    with Database.open(config.index_path) as db:  # migrating again does nothing
        assert len(db.recent_searches()) == 3


def test_unknown_scope_is_refused(db):
    with pytest.raises(ValueError):
        db.record_search("x", "", 1, "everything")
