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
    """Save text; returns its first piece, with new_entry."""
    res = client.post("/api/pieces", json=kw, headers=H)
    assert res.status_code == 201, res.text
    data = res.json()
    return {**data["pieces"][0], "new_entry": data["new_entry"]}


def test_saving_from_a_book_makes_then_adds_to_the_entry(client, db, config, kb):
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
    assert second["heading"] == "Description"  # the first on the entry's topic list
    assert second["source"] == {"type": "own"}


def test_epub_pieces_keep_the_chapter(client, db, config, kb):
    fid = file_id(db, "Modern Herbal")
    piece = add(client, title="Nettle", body="Nettle leaves sting.", file_id=fid, seq=1)
    assert piece["source"]["label"] == "TOC Leaves" and piece["source"]["chapter"]
    with NotesStore.open(config.notes_path) as store:
        assert store.get_piece(piece["id"]).href == "OEBPS/text/ch2.xhtml"


def test_other_sources_and_bad_requests(client, db, kb):
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


def test_a_book_source_cant_be_forged(client, db, config, kb):
    piece = add(client, title="Nettle", body="x", file_id=file_id(db, "Culpeper"), seq=1,
                source_ref="ignored", source_url="https://example.org")
    with NotesStore.open(config.notes_path) as store:
        saved = store.get_piece(piece["id"])
    assert (saved.source_type, saved.source_ref, saved.source_url) == ("book", None, None)


def test_list_lookup_titles_and_get(client, db, kb):
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
    assert res.status_code == 200 and [p["body"] for p in res.json()["pieces"]] == ["a, rewritten"]
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


def test_headings_typed_in_the_text(client, db, kb):
    fid = file_id(db, "Culpeper")
    res = client.post("/api/pieces", json={
        "title": "nettle", "file_id": fid, "seq": 1,
        "body": "Stings.\n# tea\nA cup a day.\n\n#Hair rinse\nFor shine.\n# Tincture\n",
    }, headers=H)
    assert res.status_code == 201
    data = res.json()
    assert (data["entry_title"], data["new_entry"]) == ("Nettle", True)
    assert [(p["heading"], p["body"], p["source"]["type"]) for p in data["pieces"]] == [
        ("Description", "Stings.", "book"), ("Tea", "A cup a day.", "book"), ("Hair rinse", "For shine.", "book")]

    # A new heading goes on the end of the entry's topic list (the book's topic), and only that one.
    names = [h["name"] for h in client.get("/api/headings?topic=Herbalism").json()["headings"]]
    assert names[:2] == ["Description", "Identification"] and names[-1] == "Hair rinse"
    assert "Hair rinse" not in [h["name"] for h in client.get("/api/headings").json()["headings"]]
    assert client.get("/api/entries/lookup?title=nettle").json()["headings"] == names
    # A title that would make a new entry is offered the given topic's list.
    assert client.get("/api/entries/lookup?title=Water&topic=Survival").json() == {"entry": None, "headings": []}

    # Editing: a # line first moves the text, and further ones add text.
    tea = data["pieces"][1]
    res = client.put(f"/api/pieces/{tea['id']}", json={"body": "# Infused oil\nIn olive oil.\n# Tea\nTwo cups."},
                     headers=H).json()
    assert [(p["heading"], p["body"], p["source"]["type"]) for p in res["pieces"]] == [
        ("Infused oil", "In olive oil.", "book"), ("Tea", "Two cups.", "book")]
    entry = client.get(f"/api/entries/{data['entry_id']}").json()
    assert [s["heading"] for s in entry["sections"]] == ["Description", "Tea", "Infused oil", "Hair rinse"]

    res = client.put(f"/api/pieces/{tea['id']}", json={"body": "# Tea\n"}, headers=H)
    assert res.status_code == 400 and "delete it" in res.json()["error"]
    res = client.post("/api/pieces", json={"title": "Nettle", "body": "# Tea\n\n# Tincture"}, headers=H)
    assert res.status_code == 400 and "no text" in res.json()["error"]


def test_headings_before_there_is_a_notes_file(client, config):
    assert client.get("/api/headings?topic=Herbalism").json() == {"topic": "Herbalism", "topics": [], "headings": []}
    assert not config.notes_path.exists()


def test_topics_for_new_entries_and_moving_them(client, db):
    # A topic with no sub-headings yet needs one typed.
    res = client.post("/api/pieces", json={"title": "Water", "body": "Boil it.", "topic": "Survival"}, headers=H)
    assert res.status_code == 400 and "Survival has no sub-headings yet" in res.json()["error"]
    water = add(client, title="Water", body="# Method\nBoil it.", topic="Survival")
    assert client.get(f"/api/entries/{water['entry_id']}").json()["topic"] == "Survival"
    assert add(client, title="Water", body="A rolling boil.")["heading"] == "Method"  # now the default
    assert client.get("/api/headings?topic=survival").json()["headings"] == [{"name": "Method", "entries": 1}]
    assert "Survival" in client.get("/api/headings").json()["topics"]

    # A book's topic is used when none is given; an entry with no text gets the topic asked for.
    nettle = add(client, title="Nettle", body="# Tea\nx", file_id=file_id(db, "Culpeper"), seq=1)
    assert client.get("/api/entries/lookup?title=Nettle").json()["entry"]["topic"] == "Herbalism"
    made = client.post("/api/entries", json={"title": "Fire", "topic": "Survival"}, headers=H).json()
    assert made["topic"] == "Survival"

    res = client.put(f"/api/entries/{nettle['entry_id']}/topic", json={"topic": "Foraging"}, headers=H)
    assert res.json() == {"topic": "Foraging"}
    assert [h["name"] for h in client.get("/api/headings?topic=Foraging").json()["headings"]] == ["Tea"]
    assert client.put("/api/entries/999/topic", json={"topic": "x"}, headers=H).status_code == 404
    # A topic typed in other capitals gets the library's (or the KB's) spelling.
    assert client.put(f"/api/entries/{nettle['entry_id']}/topic", json={"topic": " woodwork "}, headers=H).json() == {
        "topic": "Woodwork"}
    assert client.post("/api/entries", json={"title": "Smoke", "topic": "SURVIVAL"}, headers=H).json()["topic"] == "Survival"
    # A new one typed in small letters gets a capital; one with capitals of its own is kept.
    assert client.post("/api/entries", json={"title": "Knots", "topic": "rope work"}, headers=H).json()["topic"] == "Rope work"
    assert client.post("/api/entries", json={"title": "Radio", "topic": "HAM radio"}, headers=H).json()["topic"] == "HAM radio"
    assert client.put(f"/api/entries/{nettle['entry_id']}/topic", json={"topic": "x"}).status_code == 403


def test_heading_list_api(client, kb):
    add(client, title="Nettle", body="# Infusion\nLeaves.")
    names = lambda: [h["name"] for h in client.get("/api/headings").json()["headings"]]  # noqa: E731
    res = client.put("/api/headings/order", json={"names": ["Infusion", "Tea"]}, headers=H)
    assert res.status_code == 200 and [h["name"] for h in res.json()["headings"]][:3] == ["Infusion", "Tea", "Description"]
    assert client.post("/api/headings/rename", json={"name": "infusion", "new_name": "Tea"}, headers=H).json() == {
        "name": "Tea", "merged": True}
    assert names()[:2] == ["Tea", "Description"]
    res = client.delete("/api/headings?name=Tea", headers=H)
    assert res.status_code == 400 and "1 entry uses" in res.json()["error"]
    assert client.delete("/api/headings?name=Cautions", headers=H).json() == {"deleted": "Cautions"}
    assert client.delete("/api/headings?name=Cautions", headers=H).status_code == 404
    assert client.put("/api/headings/order", json={"names": []}).status_code == 403
