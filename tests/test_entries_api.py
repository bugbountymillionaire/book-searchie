"""The KB entries API (KB v3 step 3)."""

import pytest
from fastapi.testclient import TestClient

from book_searchie.indexer import run_index
from book_searchie.notes import NotesStore
from book_searchie.web.app import create_app

H = {"X-Book-Searchie": "1"}


@pytest.fixture
def client(config, db):
    run_index(config, db)
    return TestClient(create_app(config, allowed_hosts=["testserver"]))


def file_id(db, title):
    return db.conn.execute("SELECT id FROM files WHERE title = ?", (title,)).fetchone()[0]


def add(client, **kw):
    res = client.post("/api/pieces", json=kw, headers=H)
    assert res.status_code == 201, res.text
    return res.json()


def test_saving_from_a_book_makes_then_adds_to_the_entry(client, db, config):
    fid = file_id(db, "Culpeper")
    first = add(client, title="Nettle", heading="tea", body="The root of black cohosh is **bitter**.",
                file_id=fid, seq=1)
    assert first["new_entry"] and (first["entry_title"], first["heading"]) == ("Nettle", "Tea")
    assert first["body_html"] == "<p>The root of black cohosh is <strong>bitter</strong>.</p>"
    assert first["source"] == {"type": "book", "book_title": "Culpeper", "label": "2", "seq": 1, "file_id": fid,
                               "missing": False, "chapter": False, "view_url": f"/view/{fid}/1"}
    with NotesStore.open(config.notes_path) as store:
        piece = store.get_piece(first["id"])
        assert (piece.path, piece.topic) == (db.get_file(fid)["path"], "Herbalism")

    second = add(client, title="nettle", body="My own words.")
    assert not second["new_entry"] and second["entry_id"] == first["entry_id"]
    assert second["heading"] == "Description"  # the first in the config
    assert second["source"] == {"type": "own"}


def test_epub_pieces_keep_the_chapter(client, db, config):
    fid = file_id(db, "Modern Herbal")
    piece = add(client, title="Nettle", body="Nettle leaves sting.", file_id=fid, seq=1)
    assert piece["source"]["label"] == "TOC Leaves" and piece["source"]["chapter"]
    with NotesStore.open(config.notes_path) as store:
        assert store.get_piece(piece["id"]).href == "OEBPS/text/ch2.xhtml"


def test_other_sources_and_bad_requests(client, db):
    other = add(client, title="Nettle", body="Top leaves only.", source_ref="Foraging walk",
                source_url="https://example.org/walk")
    assert other["source"] == {"type": "other", "ref": "Foraging walk", "url": "https://example.org/walk"}

    def refused(status, **kw):
        res = client.post("/api/pieces", json=kw, headers=H)
        assert res.status_code == status, res.text
        return res.json()["error"]

    assert "no text" in refused(400, title="Nettle", body="  ")
    assert "needs a title" in refused(400, title=" ", body="x")
    assert "web address" in refused(400, title="Nettle", body="x", source_url="file:///c:/secret")
    assert "book and page" in refused(400, title="Nettle", body="x", file_id=file_id(db, "Culpeper"))
    assert "isn't in the index" in refused(400, title="Nettle", body="x", file_id=9999, seq=1)
    assert "isn't in this book" in refused(400, title="Nettle", body="x", file_id=file_id(db, "Culpeper"), seq=999)
    assert client.post("/api/pieces", json={"title": "Nettle", "body": "x"}).status_code == 403  # no app header


def test_a_book_source_cant_be_forged(client, db, config):
    piece = add(client, title="Nettle", body="x", file_id=file_id(db, "Culpeper"), seq=1,
                source_ref="ignored", source_url="https://example.org")
    with NotesStore.open(config.notes_path) as store:
        saved = store.get_piece(piece["id"])
    assert (saved.source_type, saved.source_ref, saved.source_url) == ("book", None, None)


def test_list_lookup_titles_and_get(client, db):
    fid = file_id(db, "Culpeper")
    add(client, title="Nettle", heading="Tea", body="a", file_id=fid, seq=1)
    add(client, title="Nettle", heading="Description", body="b")
    add(client, title="Birch Tree", heading="Poultice", body="c")

    listing = client.get("/api/entries").json()
    assert [(e["title"], e["headings"], e["pieces"]) for e in listing["entries"]] == [
        ("Birch Tree", ["Poultice"], 1), ("Nettle", ["Description", "Tea"], 2)]
    assert listing["headings"][0] == "Description"
    assert [e["title"] for e in client.get("/api/entries?topic=Herbalism").json()["entries"]] == ["Nettle"]

    assert client.get("/api/entries/titles?q=n").json() == {"titles": ["Nettle"]}
    found = client.get("/api/entries/lookup?title=NETTLE").json()["entry"]
    assert (found["title"], found["headings"]) == ("Nettle", ["Description", "Tea"])
    assert client.get("/api/entries/lookup?title=Yarrow").json()["entry"] is None

    entry = client.get(f"/api/entries/{found['id']}").json()
    assert [(s["heading"], [p["body"] for p in s["pieces"]]) for s in entry["sections"]] == [
        ("Description", ["b"]), ("Tea", ["a"])]
    assert entry["sections"][1]["pieces"][0]["source"]["view_url"] == f"/view/{fid}/1"
    assert client.get("/api/entries/999").status_code == 404

    page = client.get(f"/api/pieces?file_id={fid}&seq=1").json()["pieces"]
    assert [(p["entry_title"], p["heading"], p["body"]) for p in page] == [("Nettle", "Tea", "a")]
    assert client.get(f"/api/pieces?file_id={fid}&seq=2").json() == {"pieces": []}


def test_reading_before_there_is_a_notes_file(client, config):
    assert not config.notes_path.exists()
    assert client.get("/api/entries").json()["entries"] == []
    assert client.get("/api/entries/titles?q=n").json() == {"titles": []}
    assert client.get("/api/entries/lookup?title=Nettle").json()["entry"] is None
    assert client.get("/api/entries/search?q=nettle").json() == {"total": 0, "entries": []}
    assert client.get("/api/entries/tags").json() == {"tags": []}
    assert not config.notes_path.exists()  # reading doesn't make one


def test_search(client):
    add(client, title="Yarrow", heading="Tea", body="Mix with nettle & <honey>.")
    add(client, title="Nettle", heading="Description", body="Stings.")
    res = client.get("/api/entries/search?q=nettle").json()
    assert res["total"] == 2
    nettle, yarrow = res["entries"]
    assert (nettle["title"], nettle["title_match"], nettle["found_in"]) == ("Nettle", True, [])
    assert yarrow["found_in"] == [{"heading": "Tea", "snippet_html": "Mix with <mark>nettle</mark> &amp; &lt;honey&gt;."}]
    assert client.get("/api/entries/search?q=AND").status_code == 400


def test_edit_move_and_delete_pieces(client):
    a = add(client, title="Nettle", heading="Tea", body="a")
    b = add(client, title="Nettle", heading="Tea", body="b")

    res = client.put(f"/api/pieces/{a['id']}", json={"body": "a, rewritten"}, headers=H)
    assert res.status_code == 200 and res.json()["body"] == "a, rewritten"
    assert client.put(f"/api/pieces/{a['id']}", json={"body": " "}, headers=H).status_code == 400
    assert client.put("/api/pieces/999", json={"body": "x"}, headers=H).status_code == 404

    moved = client.post(f"/api/pieces/{b['id']}/move", json={"position": 0}, headers=H).json()
    assert moved["position"] == 0
    moved = client.post(f"/api/pieces/{a['id']}/move", json={"heading": "Tincture"}, headers=H).json()
    assert moved["heading"] == "Tincture"
    moved = client.post(f"/api/pieces/{a['id']}/move", json={"title": "Yarrow"}, headers=H).json()
    assert (moved["entry_title"], moved["heading"]) == ("Yarrow", "Tincture")
    assert client.post(f"/api/pieces/{a['id']}/move", json={"position": -1}, headers=H).status_code == 422
    assert client.post("/api/pieces/999/move", json={"heading": "Tea"}, headers=H).status_code == 404

    assert client.delete(f"/api/pieces/{a['id']}", headers=H).json() == {"deleted": a["id"]}
    assert client.delete(f"/api/pieces/{a['id']}", headers=H).status_code == 404
    assert client.delete(f"/api/pieces/{b['id']}").status_code == 403


def test_entries_tags_aliases_rename_and_delete(client):
    res = client.post("/api/entries", json={"title": "Birch Tree", "tags": ["Joint pain"]}, headers=H)
    assert res.status_code == 201
    birch = res.json()
    assert (birch["title"], birch["tags"], birch["sections"]) == ("Birch Tree", ["Joint pain"], [])
    assert client.post("/api/entries", json={"title": "birch tree"}, headers=H).status_code == 400

    eid = birch["id"]
    assert client.put(f"/api/entries/{eid}/tags", json={"tags": ["Diuretic", "Joint pain"]}, headers=H).json() == {
        "tags": ["Diuretic", "Joint pain"]}
    assert client.get("/api/entries/tags?q=di").json() == {"tags": [{"name": "Diuretic", "entries": 1, "aliases": []}]}

    assert client.post(f"/api/entries/{eid}/aliases", json={"alias": "Betula"}, headers=H).json() == {
        "aliases": ["Betula"]}
    assert client.post(f"/api/entries/{eid}/aliases", json={"alias": "betula"}, headers=H).status_code == 400
    assert client.get("/api/entries/lookup?title=betula").json()["entry"]["title"] == "Birch Tree"
    assert client.delete("/api/entries/aliases?alias=Betula", headers=H).json() == {"deleted": "Betula"}
    assert client.delete("/api/entries/aliases?alias=Betula", headers=H).status_code == 404

    add(client, title="Birch", heading="Infusion", body="Leaves.")
    add(client, title="Birch", heading="Tea", body="Sap.")
    birch2 = client.get("/api/entries/lookup?title=Birch").json()["entry"]["id"]
    assert client.post(f"/api/entries/{birch2}/headings/rename", json={"heading": "infusion", "new_heading": "tea"},
                       headers=H).json() == {"heading": "Tea"}
    assert client.post(f"/api/entries/{birch2}/headings/rename", json={"heading": "Nope", "new_heading": "Tea"},
                       headers=H).status_code == 400

    # Renaming onto another entry's title merges them.
    merged = client.post(f"/api/entries/{birch2}/rename", json={"title": "birch tree"}, headers=H).json()
    assert merged == {"id": eid, "title": "Birch Tree", "merged": True}
    entry = client.get(f"/api/entries/{eid}").json()
    assert entry["aliases"] == ["Birch"]
    assert [(s["heading"], [p["body"] for p in s["pieces"]]) for s in entry["sections"]] == [("Tea", ["Sap.", "Leaves."])]
    assert client.post(f"/api/entries/{birch2}/rename", json={"title": "x"}, headers=H).status_code == 404

    assert client.delete(f"/api/entries/{eid}", headers=H).json() == {"deleted": eid}
    assert client.delete(f"/api/entries/{eid}", headers=H).status_code == 404
    assert client.get("/api/entries").json()["entries"] == []
