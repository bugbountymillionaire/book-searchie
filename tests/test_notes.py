import sqlite3

import pytest

from book_searchie import notes as notes_mod
from book_searchie.config import ConfigError, load_config
from book_searchie.notes import Note, NoteInvalid, NotesError, NotesStore, Tag
from book_searchie.search import QueryError


@pytest.fixture
def store(tmp_path):
    with NotesStore.open(tmp_path / "notes.db") as s:
        yield s


def book_note(**kw):
    values = dict(
        source_type="book", topic="Herbalism", path="/lib/Herbalism/Culpeper.pdf", seq=1, label="2",
        book_title="Culpeper", quote="The root of black cohosh is bitter.",
        tags=[Tag("plant", "Black cohosh")],
    )
    values.update(kw)
    return Note(**values)


def other_note(**kw):
    values = dict(
        source_type="other", topic="Herbalism", title="Nettle soup", source_ref="Spring foraging walk",
        source_url="https://example.org/walk", body="Pick only the **top** leaves.",
        tags=[Tag("plant", "Nettle"), Tag("preparation", "Soup")],
    )
    values.update(kw)
    return Note(**values)


def own_note(**kw):
    values = dict(
        source_type="own", topic="Woodwork", title="Glue-up order",
        body="Dry-fit everything first.\n\n- clamps ready\n- cauls waxed",
        tags=[Tag("subject", "Clamping")],
    )
    values.update(kw)
    return Note(**values)


# --- creating, reading, editing, deleting ---


def test_create_all_three_source_types(store):
    book = store.create(book_note())
    other = store.create(other_note())
    own = store.create(own_note())

    assert book.id and book.created and book.updated
    assert (book.path, book.seq, book.source_ref) == ("/lib/Herbalism/Culpeper.pdf", 1, None)
    assert (other.source_ref, other.source_url, other.path, other.seq) == (
        "Spring foraging walk", "https://example.org/walk", None, None)
    assert (own.source_ref, own.source_url, own.path) == (None, None, None)
    assert store.count() == 3
    assert [n.id for n in store.recent()] == [own.id, other.id, book.id]
    assert store.get(other.id).tags == [Tag("plant", "Nettle"), Tag("preparation", "Soup")]


def test_book_note_title_defaults_to_start_of_quote(store):
    note = store.create(book_note(quote="one two three four five six seven eight nine ten"))
    assert note.title == ""  # nothing invented is stored
    assert note.display_title == "one two three four five six seven eight…"
    assert store.create(book_note(title="Bitter root")).display_title == "Bitter root"


def test_text_is_tidied(store):
    note = store.create(own_note(title="  Glue-up  ", body="  text \n", tags=[Tag(" Subject ", "  big   clamps ")]))
    assert (note.title, note.body, note.tags) == ("Glue-up", "text", [Tag("subject", "big clamps")])


def test_update_replaces_contents_and_tags(store):
    note = store.create(other_note())
    changed = store.update(note.id, other_note(title="Nettle soup, again", tags=[Tag("plant", "nettle")]))
    assert changed.title == "Nettle soup, again"
    assert changed.tags == [Tag("plant", "Nettle")]  # keeps its first spelling
    assert changed.created == note.created
    assert [t.name for t in store.tags()] == ["Nettle"]  # "Soup" isn't used any more
    assert store.update(9999, other_note()) is None


def test_update_can_change_source_type(store):
    note = store.create(other_note())
    changed = store.update(note.id, own_note(title="Mine now"))
    assert (changed.source_type, changed.source_ref, changed.source_url) == ("own", None, None)


def test_delete(store):
    keep = store.create(own_note())
    gone = store.create(other_note())
    assert store.delete(gone.id) is True
    assert store.delete(gone.id) is False
    assert store.get(gone.id) is None
    assert store.search("nettle") == []
    assert [t.name for t in store.tags()] == ["Clamping"]
    assert store.get(keep.id) is not None


def test_for_page(store):
    first = store.create(book_note())
    second = store.create(book_note(quote="Another line"))
    store.create(book_note(seq=2))
    store.create(own_note())
    assert [n.id for n in store.for_page("/lib/Herbalism/Culpeper.pdf", 1)] == [first.id, second.id]


# --- tags ---


def test_tags_are_one_per_name_ignoring_case(store):
    store.create(other_note(tags=[Tag("plant", "Nettle")]))
    store.create(book_note(tags=[Tag("plant", "NETTLE"), Tag("plant", "nettle")]))
    store.create(own_note(tags=[Tag("subject", "nettle")]))  # a different kind is a different tag
    assert [(t.kind, t.name, t.notes) for t in store.tags()] == [("plant", "Nettle", 2), ("subject", "nettle", 1)]
    assert len(store.with_tag("plant", "nettle")) == 2
    assert store.get(store.recent()[1].id).tags == [Tag("plant", "Nettle")]


def test_tags_by_kind_and_prefix(store):
    store.create(other_note(tags=[Tag("plant", "Nettle"), Tag("plant", "Nutmeg"), Tag("plant", "Comfrey")]))
    store.create(book_note(tags=[Tag("plant", "Nettle"), Tag("condition", "Nausea")]))
    assert [t.name for t in store.tags(kind="plant")] == ["Nettle", "Comfrey", "Nutmeg"]
    assert [t.name for t in store.tags(prefix="n")] == ["Nettle", "Nausea", "Nutmeg"]
    assert [t.name for t in store.tags(kind="plant", prefix="N", limit=1)] == ["Nettle"]
    assert store.tags(prefix="%") == []  # LIKE wildcards are matched literally


# --- searching ---


def test_search_covers_every_field(store):
    book = store.create(book_note())
    other = store.create(other_note())
    own = store.create(own_note())
    assert [h.note.id for h in store.search("cohosh")] == [book.id]  # quote and tag
    assert [h.note.id for h in store.search("foraging")] == [other.id]  # source
    assert [h.note.id for h in store.search("soup")] == [other.id]  # title and tag
    assert [h.note.id for h in store.search("cauls")] == [own.id]  # body
    assert [h.note.id for h in store.search("clamping")] == [own.id]  # tag only
    assert store.search_count("nettle OR cohosh") == 2


def test_search_follows_edits(store):
    note = store.create(own_note())
    store.update(note.id, own_note(body="Sharpen the chisel."))
    assert store.search("cauls") == []
    assert [h.note.id for h in store.search("chisel")] == [note.id]


def test_search_snippet_ranking_and_topic(store):
    title_match = store.create(own_note(title="Nettle", topic="Herbalism", body="Spring greens."))
    body_match = store.create(own_note(title="Greens", topic="Herbalism", body="Some nettle in a long note " * 5))
    store.create(own_note(title="Nettle stools", topic="Woodwork"))
    hits = store.search("nettle", topic="herbalism")
    assert [h.note.id for h in hits] == [title_match.id, body_match.id]  # title matches count for more
    assert "[nettle]" in hits[1].snippet
    assert store.search_count("nettle") == 3
    assert store.search_count("nettle", topic="Herbalism") == 2


def test_search_syntax_is_the_same_as_books(store):
    store.create(other_note(body="St John's wort, 1.5 g"))
    assert len(store.search("john's")) == 1
    assert len(store.search('"top leaves"')) == 0
    assert len(store.search("nett*")) == 1
    with pytest.raises(QueryError):
        store.search("   ")
    with pytest.raises(QueryError):
        store.search("nettle AND")


# --- validation ---


@pytest.mark.parametrize(
    "note, message",
    [
        (book_note(source_type="video"), "source type"),
        (book_note(topic=" "), "topic"),
        (book_note(quote="", body=""), "quote or some text"),
        (book_note(path=""), "book and page"),
        (book_note(seq=None), "book and page"),
        (book_note(seq=-1), "whole number"),
        (book_note(seq=True), "whole number"),
        (other_note(title=""), "title"),
        (other_note(source_ref="  "), "where this came from"),
        (other_note(source_url="javascript:alert(1)"), "http"),
        (other_note(source_url="ftp://example.org/x"), "http"),
        (other_note(source_url="https://"), "http"),
        (own_note(title=""), "title"),
        (own_note(body="", quote="A line I like"), "needs some text"),
        (own_note(title="x" * 201), "too long"),
        (own_note(body="x" * 50_001), "at most"),
        (own_note(tags=[Tag("subject", "x" * 61)]), "Tags"),
    ],
)
def test_validation(store, note, message):
    with pytest.raises(NoteInvalid, match=message):
        store.create(note)
    assert store.count() == 0


def test_fields_for_another_source_type_are_dropped(store):
    note = store.create(own_note(path="/lib/x.pdf", seq=3, source_ref="ignored", source_url="https://x.org"))
    assert (note.path, note.seq, note.source_ref, note.source_url) == (None, None, None, None)
    note = store.create(book_note(source_ref="ignored", source_url="https://x.org"))
    assert (note.source_ref, note.source_url) == (None, None)


def test_invalid_update_changes_nothing(store):
    note = store.create(own_note())
    with pytest.raises(NoteInvalid):
        store.update(note.id, own_note(body=""))
    assert store.get(note.id).body == own_note().body


# --- the file and its migrations ---


def test_notes_survive_reopening(tmp_path):
    path = tmp_path / "notes.db"
    with NotesStore.open(path) as s:
        s.create(own_note())
    with NotesStore.open(path) as s:
        assert s.count() == 1
        assert s.schema_version == len(notes_mod._MIGRATIONS)
        assert [h.note.title for h in s.search("cauls")] == ["Glue-up order"]
    assert not list(tmp_path.glob("*.bak"))  # nothing to migrate, so no copy


def test_migration_keeps_notes_and_makes_a_copy(tmp_path, monkeypatch):
    path = tmp_path / "notes.db"
    with NotesStore.open(path) as s:
        note = s.create(other_note())
    monkeypatch.setattr(
        notes_mod, "_MIGRATIONS", [*notes_mod._MIGRATIONS, "ALTER TABLE notes ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0"]
    )
    with NotesStore.open(path) as s:
        assert s.schema_version == 2
        kept = s.get(note.id)
        assert (kept.title, kept.tags, kept.source_ref) == (note.title, note.tags, note.source_ref)
        assert s.conn.execute("SELECT pinned FROM notes").fetchone()[0] == 0
        assert len(s.search("nettle")) == 1

    backup = tmp_path / "notes.db.v1.bak"
    conn = sqlite3.connect(backup)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    assert conn.execute("SELECT title FROM notes").fetchall() == [("Nettle soup",)]
    conn.close()


def test_failed_migration_changes_nothing(tmp_path, monkeypatch):
    path = tmp_path / "notes.db"
    with NotesStore.open(path) as s:
        s.create(own_note())
    monkeypatch.setattr(
        notes_mod, "_MIGRATIONS",
        [*notes_mod._MIGRATIONS, "ALTER TABLE notes ADD COLUMN pinned INTEGER; ALTER TABLE no_such_table ADD COLUMN x"],
    )
    with pytest.raises(sqlite3.OperationalError):
        NotesStore.open(path)
    monkeypatch.undo()
    with NotesStore.open(path) as s:
        assert s.schema_version == 1
        assert s.count() == 1
        assert "pinned" not in [r[1] for r in s.conn.execute("PRAGMA table_info(notes)")]


def test_newer_notes_file_is_left_alone(tmp_path):
    path = tmp_path / "notes.db"
    with NotesStore.open(path) as s:
        s.create(own_note())
        s.conn.execute("PRAGMA user_version = 99")
    with pytest.raises(NotesError, match="newer version"):
        NotesStore.open(path)
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 1
    conn.close()


# --- config ---


def test_notes_config(tmp_path, monkeypatch):
    monkeypatch.delenv("BOOK_SEARCHIE_NOTES", raising=False)
    path = tmp_path / "c.toml"
    path.write_text('[library]\nroots = ["x"]\n', encoding="utf-8")
    config = load_config(path)
    assert config.notes_path == (tmp_path / "book-searchie-notes.db").resolve()
    assert config.tag_kinds == ["subject", "use"]

    path.write_text(
        '[library]\nroots = ["x"]\n[notes]\npath = "kb/my-notes.db"\ntag_kinds = ["Plant", "condition"]\n',
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.notes_path == (tmp_path / "kb" / "my-notes.db").resolve()
    assert config.tag_kinds == ["plant", "condition"]

    monkeypatch.setenv("BOOK_SEARCHIE_NOTES", str(tmp_path / "elsewhere.db"))
    assert load_config(path).notes_path == (tmp_path / "elsewhere.db").resolve()


@pytest.mark.parametrize("kinds", ["[]", '["plant", "plant"]', '["two words"]'])
def test_bad_tag_kinds(tmp_path, kinds):
    path = tmp_path / "c.toml"
    path.write_text(f'[library]\nroots = ["x"]\n[notes]\ntag_kinds = {kinds}\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="tag_kinds|tag kinds"):
        load_config(path)


def test_snippet_comes_from_the_quote_or_text(store):
    store.create(own_note(title="Nettle", body="Spring greens, cooked like spinach."))
    store.create(other_note(title="Soup", body="A good nettle soup."))
    store.create(own_note(title="Bare", body="Nothing to see.", tags=[Tag("subject", "Nettle")]))
    snippets = {h.note.title: h.snippet for h in store.search("nettle")}
    assert snippets["Nettle"] == "Spring greens, cooked like spinach."  # not the title again
    assert snippets["Soup"] == "A good [nettle] soup."
    assert snippets["Bare"] == "Nothing to see."  # only the tag matched
