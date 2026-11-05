"""Searching books, the KB or both, with the Books and KB checkboxes (KB step 6)."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from book_searchie.db import Database
from book_searchie.indexer import run_index
from book_searchie.notes import Note, NotesStore, Tag
from book_searchie.web.app import create_app, search_scope


@pytest.fixture
def client(config, db):
    run_index(config, db)
    return TestClient(create_app(config, allowed_hosts=["testserver"]))


@pytest.fixture
def notes(config, db):
    culpeper = db.conn.execute("SELECT path FROM files WHERE title = 'Culpeper'").fetchone()[0]
    with NotesStore.open(config.notes_path) as store:
        store.create(Note(source_type="book", topic="Herbalism", path=culpeper, seq=1, label="2", book_title="Culpeper",
                          quote="The root of black cohosh is bitter.", tags=[Tag("subject", "Roots")]))
        store.create(Note(source_type="other", topic="Herbalism", title="Cohosh lecture", source_ref="Herb school",
                          body="Black cohosh <b>tincture</b> dose."))
        store.create(Note(source_type="own", topic="Woodwork", title="Cohosh-stained oak", body="An odd idea."))
        yield store


def articles(page, cls):
    return page.count(f'<article class="{cls}"')


def test_search_scope_rules():
    assert search_scope(None, None) == (True, True)  # old links and bookmarks search both
    assert search_scope("1", "1") == (True, True)
    assert search_scope("1", None) == (True, False)
    assert search_scope(None, "1") == (False, True)
    assert search_scope("0", "") == (True, True)  # at least one is always searched


def test_checkboxes_in_the_search_box(client):
    page = client.get("/").text
    assert '<input type="checkbox" name="books" value="1" checked> Books' in page
    assert '<input type="checkbox" name="kb" value="1" checked> KB' in page
    assert "/static/scope.js" in page
    page = client.get("/", params={"q": "cohosh", "kb": "1"}).text
    assert 'name="books" value="1" >' in page and 'name="kb" value="1" checked>' in page  # shows what was searched


def test_both_shows_notes_above_books(client, notes):
    page = client.get("/", params={"q": "cohosh", "books": "1", "kb": "1"}).text
    assert 'Your notes <span class="dim">(3)</span>' in page
    assert articles(page, "note-hit") == 3 and articles(page, "book") == 1
    assert page.index("Your notes") < page.index('<article class="book">')
    assert "All 3 matching notes" not in page  # all of them are already shown
    assert "Black <mark>cohosh</mark> &lt;b&gt;tincture&lt;/b&gt; dose." in page  # snippets are escaped


def test_both_links_to_all_notes_when_there_are_more(client, notes):
    for n in range(3):
        notes.create(Note(source_type="own", topic="Herbalism", title=f"More cohosh {n}", body="x"))
    page = client.get("/", params={"q": "cohosh"}).text
    assert articles(page, "note-hit") == 3
    assert 'href="/?q=cohosh&amp;topic=&amp;kb=1">All 6 matching notes' in page


def test_kb_only(client, notes):
    page = client.get("/", params={"q": "cohosh", "kb": "1"}).text
    assert "3 matching notes in your KB" in page
    assert articles(page, "note-hit") == 3 and articles(page, "book") == 0
    assert '<span class="source-badge other">Other source</span>' in page


def test_kb_only_pages(client, notes, monkeypatch):
    from book_searchie.web import app as app_module

    monkeypatch.setattr(app_module, "NOTES_PER_PAGE", 2)
    page = client.get("/", params={"q": "cohosh", "kb": "1"}).text
    assert articles(page, "note-hit") == 2 and "Page 1 of 2" in page
    assert 'href="/?q=cohosh&amp;topic=&amp;kb=1&amp;page_no=2"' in page
    assert articles(client.get("/", params={"q": "cohosh", "kb": "1", "page_no": 2}).text, "note-hit") == 1


def test_books_only_is_as_before(client, notes):
    page = client.get("/", params={"q": "cohosh", "books": "1"}).text
    assert "Your notes" not in page and articles(page, "note-hit") == 0 and articles(page, "book") == 1


def test_topic_filter_applies_to_notes(client, notes):
    page = client.get("/", params={"q": "cohosh", "topic": "Woodwork"}).text
    assert 'Your notes <span class="dim">(1)</span>' in page and "Cohosh-stained oak" in page
    assert "No results for <strong>cohosh</strong> in your books in Woodwork" in page
    page = client.get("/", params={"q": "cohosh", "topic": "Gardening", "kb": "1"}).text
    assert "No notes in your KB match" in page
    assert 'href="/?q=cohosh&amp;kb=1">Search all topics' in page


def test_no_notes_file_yet(client, config):
    page = client.get("/", params={"q": "cohosh"}).text
    assert "Your notes" not in page and articles(page, "book") == 1
    assert "No notes in your KB match" in client.get("/", params={"q": "cohosh", "kb": "1"}).text
    assert not config.notes_path.exists()


def test_bad_query_in_kb_only(client, notes):
    page = client.get("/", params={"q": "cohosh AND", "kb": "1"}).text
    assert "understand that search" in page


def test_home_counts_notes(client, notes):
    assert "Search 5 books and 3 notes" in client.get("/").text


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
    assert "1 page, 3 notes" in home and ">KB</span>" in home and ">Books</span>" in home


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
