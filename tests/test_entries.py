import sqlite3

import pytest

from book_searchie import notes as notes_mod
from book_searchie.config import ConfigError, load_config
from book_searchie.entries import CONVERTED_HEADING, DEFAULT_HEADINGS, EntryInvalid, Piece
from book_searchie.notes import Note, NotesStore, Tag
from book_searchie.search import QueryError

BOOK = "/lib/Herbalism/Culpeper.epub"


@pytest.fixture
def store(tmp_path):
    with NotesStore.open(tmp_path / "notes.db") as s:
        yield s


def from_book(body, seq=3, **kw):
    return Piece(body=body, path=BOOK, seq=seq, label="Nettles", book_title="Culpeper", href="ch3.xhtml",
                 topic="Herbalism", **kw)


# --- adding text ---


def test_typing_a_title_makes_the_entry_then_adds_to_it(store):
    first = store.add_piece("Nettle", "Description", from_book("Nettles are so well known."))
    second = store.add_piece("  nettle ", "tea", Piece(body="A cup a day in spring."))

    assert store.entry_count() == 1
    assert first.entry_id == second.entry_id
    assert (first.entry_title, second.entry_title) == ("Nettle", "Nettle")  # keeps the first spelling
    assert second.heading == "Tea"  # spelt as the config has it
    entry = store.find_entry("NETTLE")
    assert entry.headings == ["Description", "Tea"]
    assert [p.body for p in entry.sections[0].pieces] == ["Nettles are so well known."]


def test_sources_are_worked_out_and_kept(store):
    book = store.add_piece("Nettle", None, from_book("From the book."))
    other = store.add_piece("Nettle", None, Piece(body="From a walk.", source_ref="Foraging walk",
                                                  source_url="https://example.org/walk"))
    own = store.add_piece("Nettle", None, Piece(body="My own words."))

    assert (book.source_type, book.path, book.seq, book.label, book.topic) == ("book", BOOK, 3, "Nettles", "Herbalism")
    assert (other.source_type, other.source_ref, other.source_url) == ("other", "Foraging walk",
                                                                       "https://example.org/walk")
    assert (own.source_type, own.path, own.source_ref) == ("own", None, None)
    # With no sub-heading, text goes under the first one in the config.
    assert {book.heading, other.heading, own.heading} == {"Description"}
    assert [p.position for p in store.find_entry("Nettle").sections[0].pieces] == [0, 1, 2]


@pytest.mark.parametrize("piece, message", [
    (Piece(body="   "), "no text"),
    (Piece(body="x" * 50_001), "at most"),
    (Piece(body="x", path=BOOK), "page it came from"),
    (Piece(body="x", path=BOOK, seq=-1), "page it came from"),
    (Piece(body="x", source_url="javascript:alert(1)"), "web address"),
    (Piece(body="x", source_ref="y" * 501), "at most"),
])
def test_bad_pieces_are_refused(store, piece, message):
    with pytest.raises(EntryInvalid, match=message):
        store.add_piece("Nettle", None, piece)
    assert store.entry_count() == 0  # nothing half-made


@pytest.mark.parametrize("title, heading, message", [
    ("", None, "needs a title"),
    ("x" * 201, None, "at most 200"),
    ("Nettle", "   ", "needs a name"),
    ("Nettle", "x" * 81, "at most 80"),
])
def test_bad_titles_and_headings_are_refused(store, title, heading, message):
    with pytest.raises(EntryInvalid, match=message):
        store.add_piece(title, heading if heading is not None else "Tea", Piece(body="x"))


def test_new_headings_can_be_typed_and_come_after_the_configured_ones(store):
    store.add_piece("Nettle", "Soup", Piece(body="Young tops."))
    store.add_piece("Nettle", "Cautions", Piece(body="Wear gloves."))
    store.add_piece("Nettle", "Dye", Piece(body="Green from the leaves."))
    store.add_piece("Nettle", "Description", Piece(body="Stings."))
    store.add_piece("Nettle", "soup", Piece(body="With potatoes."))  # the entry's own spelling wins

    entry = store.find_entry("Nettle")
    assert entry.headings == ["Description", "Cautions", "Soup", "Dye"]
    assert [p.body for p in entry.sections[2].pieces] == ["Young tops.", "With potatoes."]


def test_heading_order_follows_the_store_headings(tmp_path):
    with NotesStore.open(tmp_path / "n.db", ["Uses", "Description"]) as s:
        s.add_piece("Nettle", "Description", Piece(body="a"))
        s.add_piece("Nettle", None, Piece(body="b"))  # the first heading: Uses
        assert s.find_entry("Nettle").headings == ["Uses", "Description"]


def test_create_entry_with_tags_and_no_text(store):
    entry = store.create_entry("Birch Tree", ["Joint pain", "joint pain", "Diuretic"])
    assert (entry.title, entry.tags, entry.sections) == ("Birch Tree", ["Diuretic", "Joint pain"], [])
    with pytest.raises(EntryInvalid, match="already an entry"):
        store.create_entry("birch tree")


# --- editing, moving, deleting ---


def test_edit_piece_text_keeps_its_source(store):
    piece = store.add_piece("Nettle", "Tea", from_book("Nettles are so wel known."))
    edited = store.update_piece(piece.id, "Nettles are well known: I've rewritten this.")
    assert edited.body == "Nettles are well known: I've rewritten this."
    assert (edited.path, edited.seq, edited.source_type) == (BOOK, 3, "book")
    assert store.update_piece(999, "x") is None
    with pytest.raises(EntryInvalid):
        store.update_piece(piece.id, "  ")


def test_move_piece_to_another_heading_and_reorder(store):
    a = store.add_piece("Nettle", "Description", Piece(body="a"))
    b = store.add_piece("Nettle", "Description", Piece(body="b"))
    c = store.add_piece("Nettle", "Description", Piece(body="c"))

    store.move_piece(c.id, position=0)
    assert [p.body for p in store.find_entry("Nettle").sections[0].pieces] == ["c", "a", "b"]

    moved = store.move_piece(a.id, heading="Tea")
    assert (moved.heading, moved.position) == ("Tea", 0)
    entry = store.find_entry("Nettle")
    assert [(s.heading, [p.body for p in s.pieces]) for s in entry.sections] == [
        ("Description", ["c", "b"]), ("Tea", ["a"])]
    assert [p.position for p in entry.sections[0].pieces] == [0, 1]

    # Moving the last piece out of a sub-heading removes the sub-heading.
    store.move_piece(a.id, heading="Description", position=1)
    entry = store.find_entry("Nettle")
    assert [(s.heading, [p.body for p in s.pieces]) for s in entry.sections] == [("Description", ["c", "a", "b"])]
    assert store.move_piece(999, heading="Tea") is None
    assert b.id


def test_move_piece_to_another_entry(store):
    piece = store.add_piece("Nettle", "Tea", Piece(body="Really about yarrow."))
    moved = store.move_piece(piece.id, title="Yarrow")
    assert (moved.entry_title, moved.heading) == ("Yarrow", "Tea")
    assert store.find_entry("Nettle").sections == []  # Nettle stays, now empty
    assert store.search_entries("yarrow")[0][0].title_match


def test_delete_piece_and_entry(store):
    a = store.add_piece("Nettle", "Tea", Piece(body="a"))
    store.add_piece("Nettle", "Description", Piece(body="b"))
    store.set_entry_tags(a.entry_id, ["Hay fever"])

    assert store.delete_piece(a.id)
    assert not store.delete_piece(a.id)
    assert store.find_entry("Nettle").headings == ["Description"]
    assert store.search_entries("tea")[1] == 0

    assert store.delete_entry(a.entry_id)
    assert not store.delete_entry(a.entry_id)
    assert store.entry_count() == 0
    assert store.conn.execute("SELECT COUNT(*) FROM pieces").fetchone()[0] == 0
    assert store.conn.execute("SELECT COUNT(*) FROM sections_fts").fetchone()[0] == 0
    assert store.entry_tag_counts() == []  # unused tags go
    with pytest.raises(QueryError):
        store.search_entries("   ")


def test_rename_section_and_merge(store):
    piece = store.add_piece("Nettle", "Infusion", Piece(body="a"))
    store.add_piece("Nettle", "Tea", Piece(body="b"))
    assert store.rename_section(piece.entry_id, "infusion", "Tisane") == "Tisane"
    assert store.rename_section(piece.entry_id, "Tisane", "tea") == "Tea"
    entry = store.find_entry("Nettle")
    assert [(s.heading, [p.body for p in s.pieces]) for s in entry.sections] == [("Tea", ["b", "a"])]
    with pytest.raises(EntryInvalid, match="no sub-heading"):
        store.rename_section(piece.entry_id, "Poultice", "x")


# --- other names, renaming and merging ---


def test_other_names_find_the_entry(store):
    piece = store.add_piece("Nettle", "Tea", Piece(body="a"))
    assert store.add_entry_alias(piece.entry_id, "Urtica dioica") == ["Urtica dioica"]
    assert store.add_piece("urtica DIOICA", "Tea", Piece(body="b")).entry_id == piece.entry_id
    assert store.entry_titles("urt") == ["Nettle"]
    assert store.search_entries("urtica")[0][0].title_match

    store.create_entry("Yarrow")
    with pytest.raises(EntryInvalid, match="already a name"):
        store.add_entry_alias(piece.entry_id, "nettle")
    with pytest.raises(EntryInvalid, match="already the entry \"Yarrow\""):
        store.add_entry_alias(piece.entry_id, "yarrow")

    assert store.remove_entry_alias("URTICA dioica")
    assert not store.remove_entry_alias("Urtica dioica")
    assert store.entry_id("Urtica dioica") is None


def test_rename_entry(store):
    piece = store.add_piece("Nettel", "Tea", Piece(body="a"))
    store.add_entry_alias(piece.entry_id, "Stinging nettle")
    assert store.rename_entry(piece.entry_id, "Nettle") == ("Nettle", False)
    assert store.get_piece(piece.id).entry_title == "Nettle"
    # Renaming to one of its own other names swaps it out of the other names.
    assert store.rename_entry(piece.entry_id, "stinging nettle") == ("stinging nettle", False)
    assert store.get_entry(piece.entry_id).aliases == []


def test_rename_entry_onto_another_merges_them(store):
    a = store.add_piece("Nettle", "Tea", Piece(body="tea 1"))
    store.add_piece("Nettle", "Description", Piece(body="desc"))
    store.set_entry_tags(a.entry_id, ["Hay fever"])
    b = store.add_piece("Common nettle", "Tea", Piece(body="tea 2"))
    store.add_piece("Common nettle", "Poultice", Piece(body="poultice"))
    store.set_entry_tags(b.entry_id, ["Joint pain", "hay fever"])
    store.add_entry_alias(b.entry_id, "Urtica")

    assert store.rename_entry(b.entry_id, "nettle") == ("Nettle", True)
    assert store.entry_count() == 1
    entry = store.find_entry("Nettle")
    assert [(s.heading, [p.body for p in s.pieces]) for s in entry.sections] == [
        ("Description", ["desc"]), ("Tea", ["tea 1", "tea 2"]), ("Poultice", ["poultice"])]
    assert entry.tags == ["Hay fever", "Joint pain"]
    assert entry.aliases == ["Common nettle", "Urtica"]  # the old title is another name now
    assert [p.position for p in entry.sections[1].pieces] == [0, 1]
    hits, total = store.search_entries("poultice")
    assert total == 1 and hits[0].entry.title == "Nettle"
    assert store.conn.execute("SELECT COUNT(*) FROM sections_fts").fetchone()[0] == 3


def test_entry_tags_work_with_tag_tidying(store):
    piece = store.add_piece("Feverfew", "Description", Piece(body="Daisy-like flowers."))
    assert store.set_entry_tags(piece.entry_id, ["Headache", " migraine "]) == ["Headache", "migraine"]
    assert store.entry_tag_counts() == [("Headache", 1, []), ("migraine", 1, [])]
    store.add_alias("tag", "Headache", "Cephalalgia")
    assert store.entry_tag_counts("ceph") == [("Headache", 1, ["Cephalalgia"])]
    assert store.entry_tag_counts("mig_") == []
    assert store.search_entries("cephalalgia")[1] == 1

    # Merging two tags (the existing tag tidying) carries entry tags too.
    assert store.rename_tag("tag", "migraine", "Headache") == ("Headache", True)
    assert store.get_entry(piece.entry_id).tags == ["Headache"]
    assert store.search_entries("migraine")[1] == 1  # the old name is another name now
    assert [e.title for e in store.entry_list(tag="migraine")] == ["Feverfew"]

    store.set_entry_tags(piece.entry_id, [])
    # Kept because it has other names, as with note tags.
    assert store.resolve_tag("tag", "Cephalalgia") == "Headache"
    with pytest.raises(EntryInvalid, match="at most 60"):
        store.set_entry_tags(piece.entry_id, ["x" * 61])


# --- reading and searching ---


def test_entry_list_is_a_to_z_with_headings(store):
    store.add_piece("yarrow", "Poultice", Piece(body="a"))  # made as Yarrow
    store.add_piece("Birch Tree", "Tea", Piece(body="b"))
    store.add_piece("Birch Tree", "Description", Piece(body="c"))
    store.add_piece("Nettle", "Tea", from_book("d"))
    store.create_entry("Comfrey", ["Wounds"])

    listing = store.entry_list()
    assert [(e.title, e.headings, e.pieces) for e in listing] == [
        ("Birch Tree", ["Description", "Tea"], 2), ("Comfrey", [], 0), ("Nettle", ["Tea"], 1),
        ("Yarrow", ["Poultice"], 1)]
    assert [e.title for e in store.entry_list(tag="wounds")] == ["Comfrey"]
    assert [e.title for e in store.entry_list(tag="Nothing")] == []
    assert [e.title for e in store.entry_list(topic="herbalism")] == ["Nettle"]
    assert store.entry_titles("b") == ["Birch Tree"]
    assert store.entry_titles("50%") == []
    assert [e.title for e in store.all_entries()] == ["Birch Tree", "Comfrey", "Nettle", "Yarrow"]
    assert store.create_entry("birch sap").title == "Birch sap"
    assert store.create_entry("pH balance").title == "pH balance"  # has capitals: kept as typed


def test_search_puts_title_matches_first_and_shows_where(store):
    # Lots of mentions of nettle in Yarrow, but Nettle is the nettle entry.
    store.add_piece("Yarrow", "Tea", Piece(body="Mix with nettle. Nettle and yarrow. More nettle, nettle, nettle."))
    store.add_piece("Nettle", "Description", Piece(body="Stings when touched."))
    store.add_piece("Nettle", "Tea", Piece(body="A spring tonic."))

    hits, total = store.search_entries("nettle")
    assert total == 2
    assert [h.entry.title for h in hits] == ["Nettle", "Yarrow"]
    assert hits[0].title_match and not hits[1].title_match
    # The title matched, not the text, so no sub-headings are listed for Nettle.
    assert hits[0].sections == []
    assert [s.heading for s in hits[1].sections] == ["Tea"]
    assert "[nettle]" in hits[1].sections[0].snippet.lower()

    hits, _ = store.search_entries("nettle tonic")
    assert [(h.entry.title, [s.heading for s in h.sections]) for h in hits] == [("Nettle", ["Tea"])]
    hits, _ = store.search_entries("tea")  # a sub-heading's name counts
    assert {h.entry.title: [s.heading for s in h.sections] for h in hits} == {"Nettle": ["Tea"], "Yarrow": ["Tea"]}

    hits, total = store.search_entries("nettle", limit=1, offset=1)
    assert (total, [h.entry.title for h in hits]) == (2, ["Yarrow"])
    with pytest.raises(QueryError):
        store.search_entries("AND")


def test_search_by_topic(store):
    store.add_piece("Nettle", "Tea", from_book("Nettle tea."))
    store.add_piece("Nettle beds", "Description", Piece(body="Nettles by the shed.", topic="Gardening"))
    assert [h.entry.title for h in store.search_entries("nettle*", topic="Herbalism")[0]] == ["Nettle"]


def test_pieces_for_page(store):
    store.add_piece("Nettle", "Tea", from_book("one"))
    store.add_piece("Dock", "Description", from_book("two"))
    store.add_piece("Dock", "Description", from_book("elsewhere", seq=4))
    assert [(p.entry_title, p.heading, p.body) for p in store.pieces_for_page(BOOK, 3)] == [
        ("Nettle", "Tea", "one"), ("Dock", "Description", "two")]


def test_book_pieces_follow_their_books(store):
    store.add_piece("Nettle", "Tea", from_book("one"))
    store.move_book(BOOK, "/lib/Herbs/Culpeper's Herbal.epub", "Herbs", "Culpeper's Herbal")
    piece = store.pieces_for_page("/lib/Herbs/Culpeper's Herbal.epub", 3)[0]
    assert (piece.topic, piece.book_title, piece.source_missing) == ("Herbs", "Culpeper's Herbal", False)

    store.sync_books(set())
    assert store.get_piece(piece.id).source_missing
    store.sync_books({"/lib/Herbs/Culpeper's Herbal.epub"})
    assert not store.get_piece(piece.id).source_missing


# --- converting old notes ---


def _v2_file(path):
    """A notes file as version 2 left it, with three notes: made now, then with the
    version 3 tables taken out again."""
    store = NotesStore.open(path)
    notes = [
        Note(source_type="book", topic="Herbalism", title="Nettles for joint aches",
             quote="An handful of the leaves of green Nettles.", body="Try it.", path=BOOK, seq=3,
             label="Nettles", book_title="Culpeper", href="ch3.xhtml", tags=[Tag("subject", "Nettle")]),
        Note(source_type="book", topic="Herbalism", quote="The leaves being gently rubbed on any place stung "
             "by a nettle or a bee, take away the pain.", path=BOOK, seq=4,
             tags=[Tag("use", "Stings"), Tag("subject", "nettle")]),
        Note(source_type="other", topic="Woodwork", title="nettles for joint aches", source_ref="A class",
             body="Same title, other capitals.", tags=[Tag("use", "Joint pain")]),
    ]
    for note in notes:
        store.create(note)
    store.add_alias("subject", "Nettle", "Urtica dioica")
    assert store.entry_count() == 0  # notes made now aren't entries
    store.conn.executescript(
        "DROP TABLE entries_fts; DROP TABLE sections_fts; DROP TABLE entry_tags; DROP TABLE pieces;"
        " DROP TABLE sections; DROP TABLE entry_aliases; DROP TABLE entries; DROP TABLE headings;"
        " PRAGMA user_version = 2;"
    )
    store.close()


def test_upgrading_converts_notes_to_entries(tmp_path):
    path = tmp_path / "notes.db"
    _v2_file(path)
    with NotesStore.open(path) as s:
        assert s.schema_version == len(notes_mod._MIGRATIONS)
        assert s.count() == 3  # the notes are kept for now
        assert [e.title for e in s.entry_list()] == [
            "Nettles for joint aches", "The leaves being gently rubbed on any place…"]

        entry = s.find_entry("NETTLES FOR JOINT ACHES")
        assert entry.headings == [CONVERTED_HEADING]
        assert s.headings == [*DEFAULT_HEADINGS, CONVERTED_HEADING]  # the config list, then the rest
        first, second = entry.sections[0].pieces
        assert first.body == "An handful of the leaves of green Nettles.\n\nTry it."
        assert (first.source_type, first.path, first.seq, first.label, first.href, first.topic) == (
            "book", BOOK, 3, "Nettles", "ch3.xhtml", "Herbalism")
        assert (second.source_type, second.source_ref, second.topic) == ("other", "A class", "Woodwork")
        # Kinds of tag become one plain list; "Nettle" and "nettle" are one tag.
        assert entry.tags == ["Joint pain", "Nettle"]
        assert s.find_entry("The leaves being gently rubbed on any place…").tags == ["Nettle", "Stings"]
        assert s.resolve_tag("tag", "urtica dioica") == "Nettle"  # other names come too
        assert s.search_entries("urtica")[1] == 2
        assert s.search_entries("handful")[1] == 1
        assert s.pieces_for_page(BOOK, 4)[0].entry_title.startswith("The leaves")
    assert (tmp_path / "notes.db.v2.bak").is_file()

    with NotesStore.open(path) as s:  # nothing is converted twice
        assert s.conn.execute("SELECT COUNT(*) FROM pieces").fetchone()[0] == 3
        assert s._convert_notes() == 0


def test_a_failed_upgrade_leaves_the_file_as_it_was(tmp_path, monkeypatch):
    path = tmp_path / "notes.db"
    _v2_file(path)

    def broken(store):
        notes_mod.migrate_to_entries(store)
        raise RuntimeError("power cut")

    monkeypatch.setattr(notes_mod, "_MIGRATIONS", [*notes_mod._MIGRATIONS[:2], broken])
    with pytest.raises(RuntimeError):
        NotesStore.open(path)
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'entries'").fetchone() is None
    assert conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 3
    conn.close()


# --- config ---


def test_headings_config(tmp_path):
    cfg = tmp_path / "book-searchie.toml"
    cfg.write_text('[library]\nroots = ["lib"]\n', encoding="utf-8")
    assert load_config(cfg).headings == DEFAULT_HEADINGS
    cfg.write_text('[library]\nroots = ["lib"]\n[notes]\nheadings = ["  Uses ", "Growing  well"]\n', encoding="utf-8")
    assert load_config(cfg).headings == ["Uses", "Growing well"]
    for bad, message in [("[]", "at least one"), ('["Tea", "tea"]', "twice"), (f'["{"x" * 81}"]', "at most")]:
        cfg.write_text(f'[library]\nroots = ["lib"]\n[notes]\nheadings = {bad}\n', encoding="utf-8")
        with pytest.raises(ConfigError, match=message):
            load_config(cfg)


# --- sub-headings typed in the text, and the master list ---


def test_split_headings():
    from book_searchie.entries import split_headings

    assert split_headings("Plain text.") == [(None, "Plain text.")]
    assert split_headings("Intro\n# Tea \nA cup.\n  ## Tincture ##\nDrops.\n#\tPoultice\n\n") == [
        (None, "Intro"), ("Tea", "A cup."), ("Tincture", "Drops.")]
    assert split_headings("#Tea\r\nA cup.\r\n\r\nMore.") == [("Tea", "A cup.\r\n\r\nMore.".replace("\r", ""))]
    assert split_headings("# \nText") == [(None, "# \nText")]  # a # with no name is just text
    assert split_headings("   \n# Tea\n") == []


def test_add_text_with_headings(store):
    pieces, new = store.add_text("nettle", "Stings.\n# tea\nA cup.\n# Hair rinse\nFor shine.", from_book(""))
    assert new
    assert [(p.entry_title, p.heading, p.body, p.source_type) for p in pieces] == [
        ("Nettle", "Description", "Stings.", "book"), ("Nettle", "Tea", "A cup.", "book"),
        ("Nettle", "Hair rinse", "For shine.", "book")]
    assert store.headings[-1] == "Hair rinse"  # remembered at the end of the list
    _, new = store.add_text("Nettle", "# hair RINSE\nAgain.")
    assert not new
    assert store.find_entry("Nettle").headings == ["Description", "Tea", "Hair rinse"]
    assert store.heading_counts()[-1] == ("Hair rinse", 1)
    with pytest.raises(EntryInvalid, match="no text"):
        store.add_text("Nettle", "# Tea\n")
    with pytest.raises(EntryInvalid, match="at most 80"):
        store.add_text("Nettle", "# " + "x" * 81 + "\nText")
    assert store.conn.execute("SELECT COUNT(*) FROM pieces").fetchone()[0] == 4  # nothing half-saved


def test_entries_show_headings_in_the_lists_order(store):
    store.add_text("Nettle", "# Cautions\na\n# Tea\nb\n# Description\nc")
    assert store.find_entry("Nettle").headings == ["Description", "Tea", "Cautions"]
    # Reordering the list (the Sub-headings page, later) reorders every entry.
    store.conn.execute("UPDATE headings SET position = -1 WHERE name = 'Cautions'")
    assert store.find_entry("Nettle").headings == ["Cautions", "Description", "Tea"]
    assert store.entry_list()[0].headings == ["Cautions", "Description", "Tea"]
    assert store.default_heading == "Cautions"


def test_edit_text(store):
    (piece,), _ = store.add_text("Nettle", "# Tea\nA cup.", from_book(""))
    edited = store.edit_text(piece.id, "A cup, rewritten.")
    assert [(p.heading, p.body) for p in edited] == [("Tea", "A cup, rewritten.")]
    edited = store.edit_text(piece.id, "# Tincture\nDrops.\n# Poultice\nLeaves.")
    assert [(p.heading, p.body, p.path) for p in edited] == [("Tincture", "Drops.", BOOK), ("Poultice", "Leaves.", BOOK)]
    assert store.find_entry("Nettle").headings == ["Tincture", "Poultice"]  # Tea had nothing left
    assert store.edit_text(999, "x") is None
    with pytest.raises(EntryInvalid, match="delete it"):
        store.edit_text(piece.id, "# Tea")


def test_new_file_heading_list_comes_from_the_config(tmp_path):
    with NotesStore.open(tmp_path / "n.db", ["Uses", "Growing"]) as s:
        assert s.headings == ["Uses", "Growing"]
    with NotesStore.open(tmp_path / "n.db", ["Changed"]) as s:
        assert s.headings == ["Uses", "Growing"]  # after that, the list is in the KB
