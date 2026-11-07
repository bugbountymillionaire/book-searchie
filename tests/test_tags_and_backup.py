"""Tidying tags and entries, other names, the research list, exporting and restoring the KB."""

import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from book_searchie.cli import main
from book_searchie.entries import Piece
from book_searchie.indexer import run_index
from book_searchie.notes import Note, NoteInvalid, NotesStore, Tag
from book_searchie.notes_backup import RestoreError, export_json, export_markdown_per_entry, restore
from book_searchie.web.app import create_app

H = {"X-Book-Searchie": "1"}


@pytest.fixture
def store(config):
    with NotesStore.open(config.notes_path) as s:
        yield s


def own(title, *tags, **kw):
    return Note(source_type="own", topic="Herbalism", title=title, body=kw.pop("body", f"About {title}."),
                tags=[Tag(*t) for t in tags], **kw)


def names(store, kind):
    return [(t.name, t.notes, t.aliases) for t in store.tags(kind=kind, by_name=True)]


# --- renaming and merging ---


def test_rename_a_tag(store):
    a = store.create(own("Soup", ("plant", "Netle")))
    assert store.rename_tag("plant", "netle", "Nettle") == ("Nettle", False)
    assert store.get(a.id).tags == [Tag("plant", "Nettle")]
    assert names(store, "plant") == [("Nettle", 1, [])]
    assert [h.note.id for h in store.search("nettle")] == [a.id]  # the search index has the new name
    assert store.search("netle") == []


def test_rename_can_change_capitals(store):
    store.create(own("Soup", ("plant", "nettle")))
    assert store.rename_tag("plant", "nettle", "Nettle") == ("Nettle", False)
    assert names(store, "plant") == [("Nettle", 1, [])]


def test_renaming_to_an_existing_tag_merges(store):
    a = store.create(own("Soup", ("plant", "Stinging nettle")))
    b = store.create(own("Tea", ("plant", "Nettle"), ("plant", "Stinging nettle")))
    c = store.create(own("Rash", ("plant", "Nettle")))
    assert store.rename_tag("plant", "Stinging nettle", "nettle") == ("Nettle", True)
    assert [store.get(n.id).tags for n in (a, b, c)] == [[Tag("plant", "Nettle")]] * 3
    assert names(store, "plant") == [("Nettle", 3, ["Stinging nettle"])]  # the old name finds it still
    store.create(own("Later", ("plant", "stinging nettle")))
    assert names(store, "plant") == [("Nettle", 4, ["Stinging nettle"])]
    assert len(store.search('"stinging nettle"')) == 4


def test_merging_keeps_the_other_tags_aliases(store):
    store.create(own("A", ("plant", "Nettle")))
    store.create(own("B", ("plant", "Urtica")))
    store.add_alias("plant", "Urtica", "Urtica dioica")
    store.rename_tag("plant", "Urtica", "Nettle")
    assert names(store, "plant") == [("Nettle", 2, ["Urtica", "Urtica dioica"])]


def test_rename_errors(store):
    store.create(own("A", ("plant", "Nettle")))
    with pytest.raises(NoteInvalid, match="no plant tag"):
        store.rename_tag("plant", "Dock", "Sorrel")
    with pytest.raises(NoteInvalid, match="needs a name"):
        store.rename_tag("plant", "Nettle", "  ")
    with pytest.raises(NoteInvalid, match="at most"):
        store.rename_tag("plant", "Nettle", "x" * 61)
    assert store.rename_tag("plant", "Nettle", "Nettle") == ("Nettle", False)  # nothing to do


def test_kinds_are_kept_apart(store):
    store.create(own("A", ("plant", "Nettle"), ("use", "Nettle")))
    store.rename_tag("plant", "Nettle", "Urtica")
    assert names(store, "plant") == [("Urtica", 1, [])] and names(store, "use") == [("Nettle", 1, [])]


# --- other names ---


def test_aliases(store):
    a = store.create(own("Soup", ("plant", "Nettle")))
    assert store.add_alias("plant", "nettle", "Urtica dioica") == ["Urtica dioica"]
    assert [h.note.id for h in store.search("urtica")] == [a.id]  # searching an alias finds the tag
    assert [n.id for n in store.with_tag("plant", "urtica dioica")] == [a.id]  # and browsing it
    assert store.resolve_tag("plant", "URTICA DIOICA") == "Nettle"
    assert [t.name for t in store.tags(prefix="urt")] == ["Nettle"]  # and autocomplete
    b = store.create(own("Tea", ("plant", "Urtica dioica")))  # saving with an alias uses the tag
    assert store.get(b.id).tags == [Tag("plant", "Nettle")]


def test_alias_errors(store):
    store.create(own("A", ("plant", "Nettle"), ("plant", "Dock")))
    store.add_alias("plant", "Nettle", "Urtica")
    with pytest.raises(NoteInvalid, match="already a name for this tag"):
        store.add_alias("plant", "Nettle", "nettle")
    with pytest.raises(NoteInvalid, match="rename one to the other"):
        store.add_alias("plant", "Dock", "Nettle")  # a tag of its own
    with pytest.raises(NoteInvalid, match="rename one to the other"):
        store.add_alias("plant", "Dock", "urtica")  # another tag's other name
    with pytest.raises(NoteInvalid, match="no plant tag"):
        store.add_alias("plant", "Sorrel", "Rumex")
    with pytest.raises(NoteInvalid, match="no use tag"):
        store.add_alias("use", "Nettle", "Urtica")  # kinds are separate


def test_remove_alias(store):
    a = store.create(own("A", ("plant", "Nettle")))
    store.add_alias("plant", "Nettle", "Urtica")
    assert store.remove_alias("plant", "URTICA") is True
    assert store.remove_alias("plant", "Urtica") is False
    assert store.search("urtica") == [] and store.get(a.id).tags == [Tag("plant", "Nettle")]


def test_a_tag_with_aliases_outlives_its_notes(store):
    a = store.create(own("A", ("plant", "Nettle")))
    store.add_alias("plant", "Nettle", "Urtica")
    store.delete(a.id)
    assert store.tags() == []  # not listed, as no note has it...
    b = store.create(own("B", ("plant", "Urtica")))
    assert store.get(b.id).tags == [Tag("plant", "Nettle")]  # ...but its other names still work


# --- export ---


def sample(store, db):
    """Three entries: text from a book, from another source and your own, with tags, other
    names, a research record and a sub-heading of your own."""
    culpeper = db.conn.execute("SELECT path FROM files WHERE title = 'Culpeper'").fetchone()[0]
    store.add_text("Black cohosh", "The root of black cohosh\nis bitter.",
                   Piece(body="x", path=culpeper, seq=1, label="2", book_title="Culpeper", topic="Herbalism"))
    store.add_text("Nettle", "# Soup\n- gloves\n- scissors",
                   Piece(body="x", source_ref="Walk", source_url="https://example.org/walk", topic="Herbalism"))
    store.add_text("Nettle", "# Tea\nMy own way: steep for ten minutes.")
    store.add_text("Untagged thought", "Just an idea.")
    nettle = store.find_entry("Nettle")
    store.set_entry_tags(nettle.id, ["Food", "Hay fever"])
    store.add_entry_alias(nettle.id, "Urtica dioica")
    store.add_alias("tag", "Hay fever", "Allergic rhinitis")
    store.set_researched("Nettle", culpeper, True, book_title="Culpeper", topic="Herbalism")
    store.reorder_headings(["Tea", "Description"])
    return culpeper


def test_export_json_has_everything(store, db, config):
    run_index(config, db)
    culpeper = sample(store, db)
    data = export_json(store)
    assert (data["format"], data["version"]) == ("book-searchie-notes", 2)
    assert data["headings"][:2] == ["Tea", "Description"] and "Soup" in data["headings"]
    by_title = {e["title"]: e for e in data["entries"]}
    assert list(by_title) == ["Black cohosh", "Nettle", "Untagged thought"]
    [book] = by_title["Black cohosh"]["sections"][0]["pieces"]
    assert book["source"] == {"path": culpeper, "seq": 1, "label": "2", "book_title": "Culpeper", "href": None,
                              "missing": False}
    assert book["source_type"] == "book" and book["topic"] == "Herbalism"
    nettle = by_title["Nettle"]
    assert (nettle["aliases"], nettle["tags"]) == (["Urtica dioica"], ["Food", "Hay fever"])
    assert [s["heading"] for s in nettle["sections"]] == ["Tea", "Soup"]  # the list's order
    assert nettle["sections"][1]["pieces"][0]["source"] == {"ref": "Walk", "url": "https://example.org/walk"}
    assert "source" not in nettle["sections"][0]["pieces"][0]
    assert {"kind": "tag", "tag": "Hay fever", "alias": "Allergic rhinitis"} in data["aliases"]
    assert data["research"] == [{"term": "nettle", "path": culpeper, "book_title": "Culpeper", "topic": "Herbalism",
                                 "done": data["research"][0]["done"], "missing": False}]


def test_markdown_per_entry(store, db, config):
    run_index(config, db)
    sample(store, db)
    store.add_text('Tea/tisane: "hot"', "x")
    files = export_markdown_per_entry(store)
    assert sorted(files) == ["Black cohosh.md", "Nettle.md", "Tea_tisane_ _hot_.md", "Untagged thought.md"]
    assert files["Nettle.md"] == (
        "# Nettle\n\nAlso called: Urtica dioica\n\nTags: Food, Hay fever\n\n"
        "## Tea\n\nMy own way: steep for ten minutes.\n\n"
        "## Soup\n\n*Source: [Walk](https://example.org/walk)*\n\n- gloves\n- scissors\n")
    assert files["Black cohosh.md"] == (
        "# Black cohosh\n\n## Description\n\n"
        "*From Culpeper, p. 2: book text, to rewrite in your own words before publishing.*\n\n"
        "The root of black cohosh\nis bitter.\n")
    left_out = export_markdown_per_entry(store, "omit")
    assert left_out["Black cohosh.md"] == "# Black cohosh\n\n*1 piece of book text left out.*\n"
    assert left_out["Nettle.md"] == files["Nettle.md"]
    with pytest.raises(ValueError):
        export_markdown_per_entry(store, "keep")


def test_file_names_that_clash_are_numbered(store):
    store.add_text("A/B", "x")
    store.add_text("A_B", "y")
    assert sorted(export_markdown_per_entry(store)) == ["A_B (2).md", "A_B.md"]


# --- restore ---


def entries_of(store):
    return [(e.id, e.title, e.aliases, e.tags, e.created, e.updated,
             [(s.heading, [(p.id, p.body, p.source_type, p.path, p.seq, p.source_url, p.created) for p in s.pieces])
              for s in e.sections]) for e in store.all_entries()]


def test_restore_round_trip(store, db, config, tmp_path):
    run_index(config, db)
    culpeper = sample(store, db)
    data = json.loads(json.dumps(export_json(store)))
    before = entries_of(store)

    with NotesStore.open(tmp_path / "restored.db") as fresh:
        assert restore(fresh, data) == 3
        assert entries_of(fresh) == before
        assert fresh.headings == store.headings
        assert fresh.resolve_tag("tag", "allergic rhinitis") == "Hay fever"
        assert fresh.researched_paths("nettle") == {culpeper}
        found, _ = fresh.search_entries("urtica")
        assert [h.entry.title for h in found] == ["Nettle"]
        assert [h.entry.title for h in fresh.search_entries("rhinitis")[0]] == ["Nettle"]  # tags' other names
        assert [h.entry.title for h in fresh.search_entries("bitter")[0]] == ["Black cohosh"]
        piece, _ = fresh.add_text("Nettle", "More.")
        assert piece[0].id == max(p[0] for e in before for s in e[6] for p in s[1]) + 1


def test_restore_a_version_1_backup_makes_entries(store, tmp_path):
    data = {"format": "book-searchie-notes", "version": 1, "aliases": [], "notes": [
        {"id": 4, "source_type": "own", "topic": "Herbalism", "title": "Nettle soup", "body": "Pick the tops.",
         "tags": [], "created": "2026-10-01T10:00:00+00:00", "updated": "2026-10-01T10:00:00+00:00"}]}
    with NotesStore.open(tmp_path / "v1.db") as fresh:
        assert restore(fresh, data) == 1
        [entry] = fresh.all_entries()
        assert entry.title == "Nettle soup" and entry.sections[0].heading == "Notes"
        assert "Pick the tops." in entry.sections[0].pieces[0].body
        assert fresh.headings[0] == "Description"


def test_restore_refuses_a_kb_with_entries_unless_replacing(store, db, config):
    run_index(config, db)
    sample(store, db)
    data = export_json(store)
    store.add_text("Only here", "x")
    with pytest.raises(RestoreError, match="already has 4 entries"):
        restore(store, data)
    assert restore(store, data, replace=True, notes_path=config.notes_path) == 3
    assert store.find_entry("Only here") is None
    copies = list(config.notes_path.parent.glob(f"{config.notes_path.name}.before-restore-*.bak"))
    assert len(copies) == 1  # what it replaced was saved first


V2 = {"format": "book-searchie-notes", "version": 2}


@pytest.mark.parametrize(
    "data, message",
    [
        ({"notes": []}, "isn't a Book-Searchie notes backup"),
        ([], "isn't a Book-Searchie notes backup"),
        ({"format": "book-searchie-notes", "version": 99, "notes": []}, "newer version"),
        ({"format": "book-searchie-notes", "version": 1, "notes": [{"source_type": "own", "topic": "x"}]},
         "Note 1 in the backup can't be restored"),
        ({**V2, "entries": [{"id": 1}]}, "Entry 1 in the backup can't be restored"),
        ({**V2, "entries": [{"title": "A", "sections": [{"heading": "Tea", "pieces": [{"body": " "}]}]}]},
         "Entry 1 in the backup can't be restored"),
        ({**V2, "entries": [{"title": "A", "sections": [{"heading": "Tea", "pieces": [
            {"body": "x", "source_type": "book"}]}]}]}, "doesn't match its type"),
        ({**V2, "entries": [{"id": 1, "title": "A"}, {"id": 1, "title": "B"}]}, "Nothing was changed"),
    ],
)
def test_bad_backups(store, data, message):
    store.add_text("Keep me", "x")
    with pytest.raises(RestoreError, match=message):
        restore(store, data, replace=True)
    assert [e.title for e in store.all_entries()] == ["Keep me"]


# --- the command line ---


def test_cli_export_and_import(store, db, config, tmp_path, capsys, monkeypatch):
    run_index(config, db)
    sample(store, db)
    store.close()
    cfg = tmp_path / "c.toml"
    cfg.write_text(f'[library]\nroots = ["{config.roots[0].as_posix()}"]\n[notes]\npath = "{config.notes_path.as_posix()}"\n',
                   encoding="utf-8")
    monkeypatch.delenv("BOOK_SEARCHIE_NOTES", raising=False)

    assert main(["--config", str(cfg), "notes", "export", str(tmp_path / "b.json")]) == 0
    assert json.loads((tmp_path / "b.json").read_text(encoding="utf-8"))["version"] == 2
    assert main(["--config", str(cfg), "notes", "export", "--format", "md", str(tmp_path / "md")]) == 0
    assert "*From Culpeper" in (tmp_path / "md" / "Black cohosh.md").read_text(encoding="utf-8")
    assert main(["--config", str(cfg), "notes", "export", "-f", "md", "--book-text", "omit", str(tmp_path / "md2")]) == 0
    assert "left out" in (tmp_path / "md2" / "Black cohosh.md").read_text(encoding="utf-8")
    assert main(["--config", str(cfg), "notes", "export", "--format", "md", "-"]) == 2  # a folder is needed
    capsys.readouterr()
    assert main(["--config", str(cfg), "notes", "export", "-"]) == 0
    assert json.loads(capsys.readouterr().out)["format"] == "book-searchie-notes"

    assert main(["--config", str(cfg), "notes", "import", str(tmp_path / "b.json")]) == 2  # KB isn't empty
    assert "--replace" in capsys.readouterr().err
    assert main(["--config", str(cfg), "notes", "import", "--replace", str(tmp_path / "b.json")]) == 0
    assert "Restored 3 entries, replacing 3" in capsys.readouterr().out
    (tmp_path / "bad.json").write_text("not json", encoding="utf-8")
    assert main(["--config", str(cfg), "notes", "import", str(tmp_path / "bad.json")]) == 2


# --- the web pages and API ---


@pytest.fixture
def client(config, db):
    run_index(config, db)
    config.tag_kinds = ["plant", "use"]
    return TestClient(create_app(config, allowed_hosts=["testserver"]))


def test_tags_page(client, store, db):
    culpeper = sample(store, db)
    page = client.get("/kb/tags").text
    assert "<h2>Tags</h2>" in page and "<h2>Entries</h2>" in page and "Your research" in page
    assert "Back up your KB" in page and 'href="/kb/headings"' in page
    assert '<a class="tidy-name" href="/kb?tag=Hay+fever">Hay fever</a>' in page
    assert 'data-alias="Allergic rhinitis"' in page
    assert "&#34;allergic rhinitis&#34;: &#34;Hay fever&#34;" in page  # for the merge warning
    nettle = store.find_entry("Nettle")
    assert f'<a class="tidy-name" href="/kb/entry/{nettle.id}">Nettle</a>' in page
    assert "&#34;urtica dioica&#34;: &#34;Nettle&#34;" in page and "2 pieces" in page
    fid = db.conn.execute("SELECT id FROM files WHERE path = ?", (culpeper,)).fetchone()[0]
    assert '<a class="tidy-name" href="/?q=nettle">“nettle”</a>' in page
    assert f'<a href="/book/{fid}?q=nettle">Culpeper</a>' in page and "book missing" not in page
    assert 'href="/kb/export?format=json"' in page and 'href="/kb/export?format=md&amp;book=omit"' in page
    assert 'href="/kb/tags"' in client.get("/kb").text


def test_tags_page_without_notes(client, config):
    page = client.get("/kb/tags").text
    assert "There's nothing in your KB to back up yet" in page and "No tags yet" in page
    assert client.get("/kb/export").status_code == 404
    assert not config.notes_path.exists()


def test_research_missing_book_and_undo(client, store, db, config, library):
    culpeper = sample(store, db)
    (library / "Herbalism" / "Culpeper.pdf").unlink()
    run_index(config, db)
    assert "book missing" in client.get("/kb/tags").text
    params = {"term": "Nettle", "path": culpeper}
    assert client.delete("/api/research", params=params).status_code == 403
    assert client.delete("/api/research", params=params, headers=H).status_code == 200
    assert client.delete("/api/research", params=params, headers=H).status_code == 404
    assert "Your research" in (page := client.get("/kb/tags").text) and "None yet" in page


def test_rename_and_alias_api(client, store, db):
    sample(store, db)
    assert client.post("/api/tags/rename", json={"kind": "tag", "name": "Food", "new_name": "x"}).status_code == 403
    res = client.post("/api/tags/rename", json={"kind": "tag", "name": "Food", "new_name": "hay fever"}, headers=H)
    assert res.json() == {"kind": "tag", "name": "Hay fever", "merged": True}
    assert store.find_entry("Nettle").tags == ["Hay fever"]
    assert [h.entry.title for h in store.search_entries("food")[0]] == ["Nettle"]  # the old name still finds it
    res = client.post("/api/tags/aliases", json={"kind": "tag", "name": "Hay fever", "alias": "Pollen"}, headers=H)
    assert res.json() == {"aliases": ["Allergic rhinitis", "Food", "Pollen"]}
    assert client.delete("/api/tags/aliases", params={"kind": "tag", "alias": "Pollen"}, headers=H).status_code == 200


def test_export_downloads(client, store, db):
    sample(store, db)
    res = client.get("/kb/export", params={"format": "json"})
    assert res.headers["content-type"] == "application/json"
    assert 'attachment; filename="book-searchie-kb-' in res.headers["content-disposition"]
    assert res.json()["version"] == 2
    res = client.get("/kb/export", params={"format": "md"})
    assert res.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(res.content)) as zf:
        assert sorted(zf.namelist()) == ["Black cohosh.md", "Nettle.md", "Untagged thought.md"]
        assert "*From Culpeper" in zf.read("Black cohosh.md").decode("utf-8")
    res = client.get("/kb/export", params={"format": "md", "book": "omit"})
    assert "book-text-left-out.zip" in res.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(res.content)) as zf:
        assert "left out" in zf.read("Black cohosh.md").decode("utf-8")
    assert client.get("/kb/export", params={"format": "pdf"}).status_code == 400
    assert client.get("/kb/export", params={"format": "md", "book": "keep"}).status_code == 400
