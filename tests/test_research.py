"""Research complete: books marked done for a search term (KB v3 step 7)."""

import pytest
from fastapi.testclient import TestClient

from book_searchie.indexer import run_index
from book_searchie.notes import NotesStore
from book_searchie.research import ResearchInvalid, research_term
from book_searchie.web.app import create_app

H = {"X-Book-Searchie": "1"}
BOOK = "/lib/Herbs/Culpeper.epub"


@pytest.fixture
def store(tmp_path):
    with NotesStore.open(tmp_path / "notes.db") as s:
        yield s


@pytest.fixture
def client(config, db):
    run_index(config, db)
    return TestClient(create_app(config, allowed_hosts=["testserver"]))


def book(db, title):
    return db.conn.execute("SELECT id, path FROM files WHERE title = ?", (title,)).fetchone()


def test_term_tidies_spaces_and_ignores_capitals():
    assert research_term("  Nettle   LEAF ") == "nettle leaf"
    assert research_term('"Black cohosh" OR root') == '"black cohosh" or root'
    with pytest.raises(ResearchInvalid):
        research_term("   ")


def test_mark_and_undo(store):
    assert not store.is_researched("nettle", BOOK)
    store.set_researched("Nettle ", BOOK, True, book_title="Culpeper", topic="Herbs")
    store.set_researched("nettle", BOOK, True)  # twice is fine
    assert store.is_researched("NETTLE", BOOK)
    assert store.researched_paths(" nettle") == {BOOK}
    assert store.researched_paths("birch") == set() and store.researched_paths("") == set()
    [record] = store.research_records()
    assert (record.term, record.book_title, record.topic, record.source_missing) == ("nettle", "Culpeper", "Herbs", False)
    store.set_researched("nettle", BOOK, False)
    assert not store.is_researched("nettle", BOOK) and store.research_records() == []


def test_records_follow_their_book(store):
    store.set_researched("nettle", BOOK, True, book_title="Culpeper", topic="Herbs")
    store.set_researched("birch", BOOK, True, book_title="Culpeper", topic="Herbs")
    new = "/lib/Old herbals/Culpeper's Herbal.epub"
    store.set_researched("birch", new, True)  # already done under the new path: kept once
    store.move_book(BOOK, new, "Old herbals", "Culpeper's Herbal")
    assert store.researched_paths("nettle") == {new} and store.researched_paths("birch") == {new}
    assert {(r.term, r.book_title) for r in store.research_records()} == {
        ("birch", ""), ("nettle", "Culpeper's Herbal")}

    store.sync_books(set())
    assert all(r.source_missing for r in store.research_records())
    store.sync_books({new})
    assert not any(r.source_missing for r in store.research_records())


def test_upgrading_keeps_a_v4_file(tmp_path):
    path = tmp_path / "notes.db"
    with NotesStore.open(path) as s:
        s.add_text("Nettle", "# Description\nStings.")
        # Back to version 4: no research, no topics, and one list of sub-headings.
        s.conn.executescript(
            "DROP TABLE research; ALTER TABLE entries DROP COLUMN topic; DROP TABLE headings;"
            " CREATE TABLE headings (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE,"
            " position INTEGER NOT NULL); INSERT INTO headings (name, position) VALUES ('Description', 0);"
            " PRAGMA user_version = 4;")
    with NotesStore.open(path) as s:
        assert s.find_entry("Nettle") is not None
        s.set_researched("nettle", BOOK, True)
        assert s.is_researched("nettle", BOOK)
    assert (tmp_path / "notes.db.v4.bak").exists()


def test_api_marks_and_undoes(client, db, config):
    fid = book(db, "Culpeper")["id"]
    res = client.put("/api/research", json={"file_id": fid, "q": " Root ", "done": True}, headers=H)
    assert res.status_code == 200 and res.json() == {"file_id": fid, "term": "root", "done": True}
    with NotesStore.open(config.notes_path) as s:
        assert s.researched_paths("root") == {book(db, "Culpeper")["path"]}
    res = client.put("/api/research", json={"file_id": fid, "q": "root", "done": False}, headers=H)
    assert res.json()["done"] is False
    with NotesStore.open(config.notes_path) as s:
        assert s.researched_paths("root") == set()


def test_api_checks(client, db, config):
    fid = book(db, "Culpeper")["id"]
    assert client.put("/api/research", json={"file_id": fid, "q": "root"}).status_code == 403
    res = client.put("/api/research", json={"file_id": 9999, "q": "root"}, headers=H)
    assert res.status_code == 400 and "index" in res.json()["error"]
    assert client.put("/api/research", json={"file_id": fid, "q": "  "}, headers=H).status_code == 400
    # Undoing with no notes file has nothing to undo, and doesn't make one.
    res = client.put("/api/research", json={"file_id": fid, "q": "root", "done": False}, headers=H)
    assert res.status_code == 200 and not config.notes_path.exists()


def test_viewer_button(client, db, config):
    fid = book(db, "Culpeper")["id"]
    page = client.get(f"/view/{fid}/1", params={"q": "Root"}).text
    assert 'id="research-done"' in page and 'aria-pressed="false"' in page
    assert ">Research complete</span>" in page and "for “root”" in page
    assert not config.notes_path.exists()  # looking doesn't make the notes file
    assert 'id="research-done"' not in client.get(f"/view/{fid}/1").text  # no search, no button

    client.put("/api/research", json={"file_id": fid, "q": "root"}, headers=H)
    page = client.get(f"/view/{fid}/1", params={"q": "root  "}).text
    assert 'aria-pressed="true"' in page and '✓ <span class="wide-only">Researched</span>' in page
    epub = book(db, "Modern Herbal")["id"]
    assert 'aria-pressed="false"' in client.get(f"/view/{epub}/0", params={"q": "root"}).text


def test_search_results_show_progress(client, db):
    page = client.get("/", params={"q": "root"}).text
    assert "books researched for" not in page and "✓ Researched</span>" not in page
    client.put("/api/research", json={"file_id": book(db, "Culpeper")["id"], "q": "ROOT"}, headers=H)
    page = client.get("/", params={"q": "root"}).text
    assert "1 of 2 books researched for “root”" in page
    assert page.count("✓ Researched</span>") == 1
    culpeper = page.split("Culpeper</a></h2>")[1].split("</header>")[0]
    assert "✓ Researched" in culpeper
    assert "books researched for" not in client.get("/", params={"q": "valerian"}).text  # another term
