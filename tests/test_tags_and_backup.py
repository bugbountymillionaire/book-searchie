"""Tidying tags, other names for tags, exporting and restoring the KB (KB step 7)."""

import json

import pytest
from fastapi.testclient import TestClient

from book_searchie.cli import main
from book_searchie.indexer import run_index
from book_searchie.notes import Note, NoteInvalid, NotesStore, Tag
from book_searchie.notes_backup import RestoreError, export_json, export_markdown, export_markdown_per_tag, restore
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
    culpeper = db.conn.execute("SELECT path FROM files WHERE title = 'Culpeper'").fetchone()[0]
    book = store.create(Note(source_type="book", topic="Herbalism", path=culpeper, seq=1, label="2",
                             book_title="Culpeper", quote="The root of black cohosh\nis bitter.",
                             tags=[Tag("plant", "Black cohosh")]))
    other = store.create(Note(source_type="other", topic="Herbalism", title="Nettle soup", source_ref="Walk",
                              source_url="https://example.org/walk", body="- gloves\n- scissors",
                              tags=[Tag("plant", "Nettle"), Tag("use", "Food")]))
    mine = store.create(own("Untagged thought"))
    store.add_alias("plant", "Nettle", "Urtica dioica")
    return book, other, mine


def test_export_json_has_everything(store, db, config):
    run_index(config, db)
    book, other, mine = sample(store, db)
    data = export_json(store)
    assert (data["format"], data["version"]) == ("book-searchie-notes", 1)
    by_id = {n["id"]: n for n in data["notes"]}
    assert by_id[book.id]["source"] == {"path": book.path, "seq": 1, "label": "2", "book_title": "Culpeper",
                                        "href": None, "missing": False}
    assert by_id[other.id]["source"] == {"ref": "Walk", "url": "https://example.org/walk"}
    assert "source" not in by_id[mine.id]
    assert by_id[other.id]["tags"] == [{"kind": "plant", "name": "Nettle"}, {"kind": "use", "name": "Food"}]
    assert data["aliases"] == [{"kind": "plant", "tag": "Nettle", "alias": "Urtica dioica"}]


def test_export_markdown(store, db, config):
    run_index(config, db)
    sample(store, db)
    md = export_markdown(store)
    assert md.startswith("# KB notes\n\n3 notes, exported from Book-Searchie on ")
    assert "## The root of black cohosh is bitter.\n\n**Book:** Culpeper, p. 2 · Herbalism\n\n> The root of black cohosh\n> is bitter." in md
    assert "**Other source:** [Walk](https://example.org/walk) · Herbalism" in md
    assert "- gloves\n- scissors" in md and "Tags: plant: Nettle; use: Food" in md
    assert "**Own knowledge** · Herbalism" in md


def test_export_markdown_per_tag(store, db, config):
    run_index(config, db)
    sample(store, db)
    store.create(own("Odd", ("use", 'Tea/tisane: "hot"')))
    files = export_markdown_per_tag(store)
    assert sorted(files) == ["plant/Black cohosh.md", "plant/Nettle.md", "untagged.md", "use/Food.md",
                             "use/Tea_tisane_ _hot_.md"]
    assert files["plant/Nettle.md"].startswith("# Nettle (plant), also called Urtica dioica\n\n1 note,")
    assert "Untagged thought" in files["untagged.md"]


# --- restore ---


def test_restore_round_trip(store, db, config, tmp_path):
    run_index(config, db)
    sample(store, db)
    data = json.loads(json.dumps(export_json(store)))
    before = [(n.id, n.title, n.tags, n.path, n.source_url, n.created, n.updated) for n in store.all_notes()]

    with NotesStore.open(tmp_path / "restored.db") as fresh:
        assert restore(fresh, data) == 3
        after = [(n.id, n.title, n.tags, n.path, n.source_url, n.created, n.updated) for n in fresh.all_notes()]
        assert after == before
        assert fresh.resolve_tag("plant", "urtica dioica") == "Nettle"
        assert len(fresh.search("urtica")) == 1 and len(fresh.search("cohosh")) == 1
        new = fresh.create(own("After restore"))
        assert new.id == max(n[0] for n in before) + 1


def test_restore_refuses_a_kb_with_notes_unless_replacing(store, db, config):
    run_index(config, db)
    sample(store, db)
    data = export_json(store)
    store.create(own("Only here"))
    with pytest.raises(RestoreError, match="already has 4 notes"):
        restore(store, data)
    assert restore(store, data, replace=True, notes_path=config.notes_path) == 3
    assert [n.title for n in store.all_notes() if n.title == "Only here"] == []
    copies = list(config.notes_path.parent.glob(f"{config.notes_path.name}.before-restore-*.bak"))
    assert len(copies) == 1  # the notes it replaced were saved first


@pytest.mark.parametrize(
    "data, message",
    [
        ({"notes": []}, "isn't a Book-Searchie notes backup"),
        ([], "isn't a Book-Searchie notes backup"),
        ({"format": "book-searchie-notes", "version": 99, "notes": []}, "newer version"),
        ({"format": "book-searchie-notes", "version": 1, "notes": [{"source_type": "own", "topic": "x"}]},
         "Note 1 in the backup can't be restored"),
        ({"format": "book-searchie-notes", "version": 1, "notes": [
            {"id": 1, "source_type": "own", "topic": "x", "title": "a", "body": "b"},
            {"id": 1, "source_type": "own", "topic": "x", "title": "c", "body": "d"}]}, "Nothing was changed"),
    ],
)
def test_bad_backups(store, data, message):
    store.create(own("Keep me"))
    with pytest.raises(RestoreError, match=message):
        restore(store, data, replace=True)
    assert [n.title for n in store.all_notes()] == ["Keep me"]


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
    assert json.loads((tmp_path / "b.json").read_text(encoding="utf-8"))["format"] == "book-searchie-notes"
    assert main(["--config", str(cfg), "notes", "export", "--format", "md", "--per-tag", str(tmp_path / "md")]) == 0
    assert (tmp_path / "md" / "plant" / "Nettle.md").is_file()
    assert main(["--config", str(cfg), "notes", "export", "--per-tag", str(tmp_path / "x")]) == 2  # json can't
    capsys.readouterr()
    assert main(["--config", str(cfg), "notes", "export", "--format", "md", "-"]) == 0
    assert capsys.readouterr().out.startswith("# KB notes")

    assert main(["--config", str(cfg), "notes", "import", str(tmp_path / "b.json")]) == 2  # KB isn't empty
    assert "--replace" in capsys.readouterr().err
    assert main(["--config", str(cfg), "notes", "import", "--replace", str(tmp_path / "b.json")]) == 0
    assert "Restored 3 notes, replacing 3" in capsys.readouterr().out
    (tmp_path / "bad.json").write_text("not json", encoding="utf-8")
    assert main(["--config", str(cfg), "notes", "import", str(tmp_path / "bad.json")]) == 2


# --- the web pages and API ---


@pytest.fixture
def client(config, db):
    run_index(config, db)
    config.tag_kinds = ["plant", "use"]
    return TestClient(create_app(config, allowed_hosts=["testserver"]))


def test_tags_page(client, store, db):
    sample(store, db)
    page = client.get("/kb/tags").text
    assert "Tidy your tags" in page and "Back up your KB" in page
    assert '<a class="tidy-name" href="/kb/tag/plant/Nettle">Nettle</a>' in page
    assert 'data-alias="Urtica dioica"' in page
    assert "&#34;urtica dioica&#34;: &#34;Nettle&#34;" in page  # for the merge warning
    assert 'href="/kb/export?format=json"' in page
    assert 'href="/kb/tags"' in client.get("/kb").text


def test_tags_page_without_notes(client):
    page = client.get("/kb/tags").text
    assert "There are no notes to back up yet" in page
    assert client.get("/kb/export").status_code == 404


def test_rename_and_alias_api(client, store, db):
    sample(store, db)
    assert client.post("/api/tags/rename", json={"kind": "plant", "name": "Nettle", "new_name": "x"}).status_code == 403
    res = client.post("/api/tags/rename", json={"kind": "plant", "name": "Black cohosh", "new_name": "nettle"}, headers=H)
    assert res.json() == {"kind": "plant", "name": "Nettle", "merged": True}
    res = client.post("/api/tags/aliases", json={"kind": "plant", "name": "Nettle", "alias": "Stinger"}, headers=H)
    assert res.json() == {"aliases": ["Black cohosh", "Stinger", "Urtica dioica"]}
    res = client.post("/api/tags/aliases", json={"kind": "plant", "name": "Nettle", "alias": "nettle"}, headers=H)
    assert res.status_code == 400 and "already a name" in res.json()["error"]
    assert client.delete("/api/tags/aliases", params={"kind": "plant", "alias": "Stinger"}).status_code == 403
    assert client.delete("/api/tags/aliases", params={"kind": "plant", "alias": "Stinger"}, headers=H).status_code == 200
    assert client.delete("/api/tags/aliases", params={"kind": "plant", "alias": "Stinger"}, headers=H).status_code == 404


def test_alias_address_goes_to_the_tag(client, store, db):
    sample(store, db)
    res = client.get("/kb/tag/plant/Urtica%20dioica", params={"by": "use"}, follow_redirects=False)
    assert res.status_code == 307 and res.headers["location"] == "/kb/tag/plant/Nettle?by=use"
    assert "<h1>Nettle" in client.get("/kb/tag/plant/urtica dioica").text


def test_export_downloads(client, store, db):
    sample(store, db)
    res = client.get("/kb/export", params={"format": "json"})
    assert res.headers["content-type"] == "application/json"
    assert 'attachment; filename="book-searchie-notes-' in res.headers["content-disposition"]
    assert res.json()["format"] == "book-searchie-notes"
    res = client.get("/kb/export", params={"format": "md"})
    assert res.headers["content-type"].startswith("text/markdown") and res.text.startswith("# KB notes")
    assert client.get("/kb/export", params={"format": "pdf"}).status_code == 400
