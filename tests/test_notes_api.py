"""The KB notes API (KB step 3)."""

import pytest
from fastapi.testclient import TestClient

from book_searchie.indexer import run_index
from book_searchie.notes import NotesStore
from book_searchie.web.app import create_app

H = {"X-Book-Searchie": "1"}


@pytest.fixture
def client(config, db):
    run_index(config, db)
    config.tag_kinds = ["plant", "condition"]
    return TestClient(create_app(config, allowed_hosts=["testserver"]))


def file_id(db, title):
    return db.conn.execute("SELECT id FROM files WHERE title = ?", (title,)).fetchone()[0]


def book(db, **kw):
    data = {"source_type": "book", "file_id": file_id(db, "Culpeper"), "seq": 1,
            "quote": "The root of black cohosh is bitter.", "tags": [{"kind": "plant", "name": "Black cohosh"}]}
    data.update(kw)
    return data


OTHER = {"source_type": "other", "title": "Nettle soup", "topic": "Herbalism", "source_ref": "Foraging walk",
         "source_url": "https://example.org/walk", "body": "Top leaves only.",
         "tags": [{"kind": "plant", "name": "Nettle"}]}
OWN = {"source_type": "own", "title": "Glue-up", "topic": "woodwork", "body": "Dry-fit first."}


def test_create_book_note_takes_its_source_from_the_index(client, db, config):
    res = client.post("/api/notes", json=book(db, topic="Woodwork"), headers=H)
    assert res.status_code == 201
    note = res.json()
    fid = file_id(db, "Culpeper")
    assert note["source_type"] == "book"
    assert (note["topic"], note["book_title"], note["seq"], note["label"]) == ("Herbalism", "Culpeper", 1, "2")
    assert (note["file_id"], note["view_url"]) == (fid, f"/view/{fid}/1")
    assert note["display_title"] == "The root of black cohosh is bitter."
    with NotesStore.open(config.notes_path) as store:
        assert store.get(note["id"]).path == db.get_file(fid)["path"]


def test_create_epub_note_keeps_the_chapter_link(client, db, config):
    res = client.post("/api/notes", json=book(db, file_id=file_id(db, "Modern Herbal"), seq=1,
                                             quote="Nettle leaves sting."), headers=H)
    note = res.json()
    assert res.status_code == 201 and note["label"] == "TOC Leaves"  # the chapter's name in the book's contents
    with NotesStore.open(config.notes_path) as store:
        assert store.get(note["id"]).href == "OEBPS/text/ch2.xhtml"


def test_create_other_and_own_notes(client):
    other = client.post("/api/notes", json=OTHER, headers=H).json()
    assert (other["source_ref"], other["source_url"], other["topic"]) == (
        "Foraging walk", "https://example.org/walk", "Herbalism")
    assert "file_id" not in other
    own = client.post("/api/notes", json=OWN, headers=H).json()
    assert own["topic"] == "Woodwork"  # matched to the library's spelling
    assert "source_ref" not in own and "file_id" not in own


def test_get_page_notes_and_one_note(client, db):
    fid = file_id(db, "Culpeper")
    assert client.get("/api/notes", params={"file_id": fid, "seq": 1}).json() == {"notes": []}  # no notes file yet
    first = client.post("/api/notes", json=book(db), headers=H).json()
    client.post("/api/notes", json=book(db, seq=2, quote="Echinacea grows tall."), headers=H)
    client.post("/api/notes", json=OWN, headers=H)
    page = client.get("/api/notes", params={"file_id": fid, "seq": 1}).json()["notes"]
    assert [n["id"] for n in page] == [first["id"]]
    assert client.get(f"/api/notes/{first['id']}").json() == first
    assert client.get("/api/notes/999").status_code == 404
    assert client.get("/api/notes", params={"file_id": 999, "seq": 0}).status_code == 404


def test_update_note(client, db):
    note = client.post("/api/notes", json=book(db), headers=H).json()
    res = client.put(f"/api/notes/{note['id']}", json={"source_type": "book", "title": "Bitter root",
                                                     "quote": note["quote"], "body": "Use sparingly."}, headers=H)
    changed = res.json()
    assert res.status_code == 200
    assert (changed["title"], changed["body"], changed["tags"]) == ("Bitter root", "Use sparingly.", [])
    assert (changed["seq"], changed["file_id"], changed["topic"]) == (1, note["file_id"], "Herbalism")  # source kept

    other = client.post("/api/notes", json=OTHER, headers=H).json()
    res = client.put(f"/api/notes/{other['id']}", json={**OTHER, "source_type": "own", "title": "Mine"}, headers=H)
    assert res.json()["source_type"] == "own" and "source_ref" not in res.json()
    assert client.put("/api/notes/999", json=OWN, headers=H).status_code == 404


def test_delete_note(client, db):
    note = client.post("/api/notes", json=OTHER, headers=H).json()
    assert client.delete(f"/api/notes/{note['id']}", headers=H).json() == {"deleted": note["id"]}
    assert client.delete(f"/api/notes/{note['id']}", headers=H).status_code == 404
    assert client.get(f"/api/notes/{note['id']}").status_code == 404


def test_tags_autocomplete(client, db):
    assert client.get("/api/tags").json() == {"kinds": ["plant", "condition"], "tags": []}
    client.post("/api/notes", json=OTHER, headers=H)
    client.post("/api/notes", json={**OWN, "tags": [{"kind": "plant", "name": "nettle"},
                                                    {"kind": "condition", "name": "Nerves"}]}, headers=H)
    assert client.get("/api/tags", params={"q": "ne"}).json()["tags"] == [
        {"kind": "plant", "name": "Nettle", "notes": 2}, {"kind": "condition", "name": "Nerves", "notes": 1}]
    assert [t["name"] for t in client.get("/api/tags", params={"kind": "condition"}).json()["tags"]] == ["Nerves"]


def test_changes_need_the_app_header(client, db):
    assert client.post("/api/notes", json=OTHER).status_code == 403
    note = client.post("/api/notes", json=OTHER, headers=H).json()
    assert client.put(f"/api/notes/{note['id']}", json=OWN).status_code == 403
    assert client.delete(f"/api/notes/{note['id']}").status_code == 403
    assert client.get(f"/api/notes/{note['id']}").json()["title"] == "Nettle soup"


@pytest.mark.parametrize(
    "change, message",
    [
        ({"file_id": None}, "book and page"),
        ({"seq": 99}, "page isn't in this book"),
        ({"file_id": 999}, "isn't in the index"),
        ({"quote": ""}, "quote or some text"),
        ({"tags": [{"kind": "colour", "name": "green"}]}, "Unknown kind of tag"),
    ],
)
def test_book_note_errors(client, db, change, message):
    res = client.post("/api/notes", json=book(db, **change), headers=H)
    assert res.status_code == 400 and message in res.json()["error"]


@pytest.mark.parametrize(
    "data, message",
    [
        ({**OTHER, "source_url": "javascript:alert(1)"}, "http"),
        ({**OTHER, "source_ref": ""}, "where this came from"),
        ({**OTHER, "topic": "Astrology"}, "library's topics"),
        ({**OTHER, "topic": ""}, "topic"),
        ({**OWN, "body": ""}, "quote or some text"),
        ({**OWN, "source_type": "dream"}, "source type"),
    ],
)
def test_other_and_own_note_errors(client, data, message):
    res = client.post("/api/notes", json=data, headers=H)
    assert res.status_code == 400 and message in res.json()["error"]


def test_malformed_request_gets_a_readable_error(client):
    res = client.post("/api/notes", json={"title": "No type", "seq": "first"}, headers=H)
    assert res.status_code == 422
    assert "source_type" in res.json()["error"] and "seq" in res.json()["error"]


def test_book_outside_the_library_is_refused(client, db, tmp_path):
    db.conn.execute("UPDATE files SET path = ? WHERE title = 'Culpeper'", (str(tmp_path / "elsewhere.pdf"),))
    db.conn.commit()
    res = client.post("/api/notes", json=book(db), headers=H)
    assert res.status_code == 400 and "isn't in the library" in res.json()["error"]


def test_topic_and_tag_kind_that_have_gone_can_stay_on_edit(client, db, config):
    note = client.post("/api/notes", json={**OTHER, "tags": [{"kind": "condition", "name": "Gout"}]}, headers=H).json()
    with NotesStore.open(config.notes_path) as store:
        store.conn.execute("UPDATE notes SET topic = 'Old topic' WHERE id = ?", (note["id"],))
        store.conn.commit()
    config.tag_kinds.remove("condition")
    res = client.put(f"/api/notes/{note['id']}",
                     json={**OTHER, "topic": "old topic", "tags": [{"kind": "condition", "name": "Gout"}]}, headers=H)
    assert res.status_code == 200 and res.json()["topic"] == "Old topic"
    res = client.post("/api/notes", json={**OTHER, "topic": "Old topic"}, headers=H)
    assert res.status_code == 400  # but new notes need a current topic


def test_missing_book_has_no_link(client, db, config, library):
    note = client.post("/api/notes", json=book(db), headers=H).json()
    (library / "Herbalism" / "Culpeper.pdf").unlink()
    run_index(config, db)
    got = client.get(f"/api/notes/{note['id']}").json()
    assert got["source_missing"] is True and got["view_url"] is None and got["book_title"] == "Culpeper"


# --- "Save to KB" in the viewer (KB step 4) ---


def test_pdf_viewer_has_the_panel_and_page_text(client, db, library, config):
    from .conftest import make_pdf

    make_pdf(library / "Herbalism" / "Html.pdf", ["Black cohosh <b>root</b> & more"])
    run_index(config, db)
    fid = file_id(db, "Html")
    page = client.get(f"/view/{fid}/0", params={"q": "cohosh"}).text
    assert 'id="kb-panel"' in page and 'id="kb-open"' in page and "/static/kb.js" in page
    assert 'data-kinds="[&#34;plant&#34;, &#34;condition&#34;]"' in page
    assert 'for="kb-tag-plant"' in page and 'for="kb-tag-condition"' in page
    assert '<div class="kb-text" id="kb-page-text" tabindex="0">Black <mark>cohosh</mark> &lt;b&gt;root&lt;/b&gt; &amp; more</div>' in page


def test_epub_viewer_has_the_panel_without_page_text(client, db):
    page = client.get(f"/view/{file_id(db, 'Modern Herbal')}/0").text
    assert 'id="kb-panel"' in page and 'id="kb-page-text"' not in page
    assert "Select text in the chapter" in page


def test_note_on_a_page_without_text(client, db, library, config):
    from .conftest import make_pdf

    make_pdf(library / "Herbalism" / "Plates.pdf", ["Plate one, nettle.", "", "Plate three."])
    run_index(config, db)
    fid = file_id(db, "Plates")
    assert "This page has no text to quote from" in client.get(f"/view/{fid}/1").text
    res = client.post("/api/notes", json=book(db, file_id=fid, seq=1, quote="", body="A drawing of nettle."), headers=H)
    assert res.status_code == 201 and res.json()["label"] == "2"
    res = client.post("/api/notes", json=book(db, file_id=fid, seq=7, body="x"), headers=H)
    assert res.status_code == 400 and "isn't in this book" in res.json()["error"]
