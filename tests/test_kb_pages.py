"""The KB pages (KB step 5)."""

import pytest
from fastapi.testclient import TestClient

from book_searchie.indexer import run_index
from book_searchie.entries import Piece
from book_searchie.notes import Note, NotesStore, Tag
from book_searchie.web.app import create_app
from .conftest import seed_headings


@pytest.fixture
def client(config, db):
    run_index(config, db)
    config.tag_kinds = ["plant", "condition"]
    return TestClient(create_app(config, allowed_hosts=["testserver"]))


@pytest.fixture
def notes(config):
    with NotesStore.open(config.notes_path) as store:
        seed_headings(store)
        yield store


def file_row(db, title):
    return db.conn.execute("SELECT id, path FROM files WHERE title = ?", (title,)).fetchone()


def add_samples(notes, db):
    culpeper = file_row(db, "Culpeper")
    herbal = file_row(db, "Modern Herbal")
    return {
        "book": notes.create(Note(
            source_type="book", topic="Herbalism", path=culpeper["path"], seq=1, label="2", book_title="Culpeper",
            quote="The root of black cohosh is bitter.",
            tags=[Tag("plant", "Black cohosh"), Tag("condition", "Hot flushes")])),
        "epub": notes.create(Note(
            source_type="book", topic="Herbalism", path=herbal["path"], seq=1, label="Leaves",
            href="OEBPS/text/ch2.xhtml", book_title="Modern Herbal", quote="Nettle leaves sting.",
            tags=[Tag("plant", "Nettle"), Tag("condition", "Arthritis")])),
        "other": notes.create(Note(
            source_type="other", topic="Herbalism", title="Nettle soup", source_ref="Spring foraging walk",
            source_url="https://example.org/walk", body="Pick the **top** leaves.\n\n- gloves\n- scissors",
            tags=[Tag("plant", "Nettle")])),
        "own": notes.create(Note(
            source_type="own", topic="Woodwork", title="Glue-up order", body="Dry-fit <everything> first.",
            tags=[Tag("plant", "Oak")])),
    }


def test_header_links_to_the_kb(client):
    page = client.get("/").text
    assert '<a href="/kb"' in page


def test_empty_kb(client, config):
    page = client.get("/kb")
    assert page.status_code == 200 and "Your KB is made of" in page.text
    assert 'id="kb-new-entry"' in page.text and "Save to KB" in page.text
    assert 'href="/kb/headings"' in page.text
    assert not config.notes_path.exists()  # looking doesn't create the notes file


def add_entries(notes, db):
    culpeper = file_row(db, "Culpeper")
    notes.add_text("Nettle", "Stinging hairs on the <leaves>.\n# Tea\nPour on **boiling** water.",
                   Piece(body="x", path=culpeper["path"], seq=1, label="2", book_title="Culpeper", topic="Herbalism"))
    notes.add_text("Birch", "# Sap\nTapped in early spring.")
    nettle = notes.find_entry("Nettle")
    notes.add_entry_alias(nettle.id, "Urtica dioica")
    notes.set_entry_tags(nettle.id, ["Rose / hip & haw"])
    return nettle, notes.find_entry("Birch"), culpeper["id"]


def test_kb_lists_entries_and_opens_the_first(client, notes, db):
    nettle, birch, culpeper = add_entries(notes, db)
    page = client.get("/kb").text
    assert 'aria-current="page">KB</a>' in page
    assert page.index(f'href="/kb/entry/{birch.id}"') < page.index(f'href="/kb/entry/{nettle.id}"')  # A to Z
    assert 'data-names="nettle\nurtica dioica"' in page
    assert f'<h1>Birch</h1>' in page and "Tapped in early spring." in page  # the first entry is open
    assert f'<a href="/kb/entry/{nettle.id}#h-tea">Tea</a>' in page


def test_entry_page(client, notes, db):
    nettle, _, culpeper = add_entries(notes, db)
    page = client.get(f"/kb/entry/{nettle.id}").text
    assert "<title>Nettle · KB" in page and "<h1>Nettle</h1>" in page
    assert 'data-alias="Urtica dioica"' in page
    assert 'href="/kb?tag=Rose+%2F+hip+%26+haw"' in page
    # Sub-headings in the list's order, the first open; text formatted and escaped; links to the book.
    assert page.index('id="h-description"') < page.index('id="h-tea"')
    assert '<details class="kb-section" id="h-description" data-heading="Description" open>' in page
    assert "<p>Pour on <strong>boiling</strong> water.</p>" in page and "&lt;leaves&gt;" in page
    assert f'href="/view/{culpeper}/1"' in page
    assert client.get("/kb/entry/999").status_code == 404


def test_kb_parts_for_the_script(client, notes, db):
    nettle, _, _ = add_entries(notes, db)
    entry = client.get(f"/kb/entry/{nettle.id}", params={"part": "entry"}).text
    assert entry.lstrip().startswith("<article") and "<html" not in entry
    listing = client.get(f"/kb/entry/{nettle.id}", params={"part": "list"}).text
    assert listing.lstrip().startswith("<details") and "<html" not in listing


def test_kb_search_and_tag(client, notes, db):
    nettle, birch, _ = add_entries(notes, db)
    page = client.get("/kb", params={"q": "boiling"}).text
    assert "1 entry for <strong>boiling</strong>" in page
    assert f'href="/kb/entry/{nettle.id}?q=boiling#h-tea"' in page and "<mark>boiling</mark>" in page
    assert 'data-q="boiling"' in page and "<h1>Nettle</h1>" in page
    assert "Birch</a>" not in page
    assert client.get("/kb", params={"q": '"unclosed'}).status_code == 200

    tagged = client.get("/kb", params={"tag": "rose / HIP & haw"}).text
    assert "Tagged <strong>Rose / hip &amp; haw</strong>" in tagged
    assert f'href="/kb/entry/{nettle.id}?tag=Rose' in tagged and f'/kb/entry/{birch.id}' not in tagged


def test_headings_page(client, notes, db):
    add_entries(notes, db)
    page = client.get("/kb/headings?topic=Herbalism").text
    assert "<h1>Sub-headings for Herbalism</h1>" in page and "kb_headings.js" in page
    assert 'data-topic="Herbalism"' in page
    tea = page.split('data-name="Tea"')[1].split("</li>")[0]
    assert "1 entry" in tea and 'data-act="remove"' not in tea  # in use: can't remove
    assert 'data-name="Sap"' not in page  # Birch has no topic, so Sap is on that list
    sap = client.get("/kb/headings?topic=").text.split('data-name="Sap"')[1].split("</li>")[0]
    assert 'data-act="rename"' in sap
    # With no topic asked for, the first topic with sub-headings.
    assert "<h1>Sub-headings for Gardening</h1>" in client.get("/kb/headings").text


def test_headings_page_before_any_notes(client, config):
    page = client.get("/kb/headings").text
    assert "<h1>Sub-headings</h1>" in page and "data-name=" not in page and not config.notes_path.exists()


def test_book_notes_title_defaults_to_the_quote(client, notes, db):
    n = add_samples(notes, db)
    page = client.get(f"/kb/note/{n['book'].id}").text
    assert "<title>The root of black cohosh is bitter. · KB" in page


def test_missing_book(client, notes, db, config, library):
    n = add_samples(notes, db)
    (library / "Herbalism" / "Culpeper.pdf").unlink()
    run_index(config, db)
    page = client.get(f"/kb/note/{n['book'].id}").text
    assert "Culpeper, p. 2" in page and "/view/" not in page.split('class="note-source"')[1].split("</p>")[0]
    assert ">source missing</span>" in page


def test_tag_page_groups_by_another_kind(client, notes, db):
    n = add_samples(notes, db)
    page = client.get("/kb/tag/plant/nettle").text  # any capitals
    assert "<h1>Nettle" in page and "2 notes." in page
    assert '<a href="/kb/tag/condition/Arthritis">Arthritis</a>' in page
    assert "No condition" in page
    assert page.index(f'id="note-{n["epub"].id}"') < page.index("No condition") < page.index(f'id="note-{n["other"].id}"')

    ungrouped = client.get("/kb/tag/plant/Nettle", params={"by": "none"}).text
    assert "No condition" not in ungrouped and ungrouped.count('class="note-card"') == 2

    by_plant = client.get("/kb/tag/condition/Arthritis").text
    assert '<a href="/kb/tag/plant/Nettle">Nettle</a>' in by_plant


def test_tag_with_awkward_characters(client, notes):
    notes.create(Note(source_type="own", topic="Herbalism", title="Mix", body="x",
                      tags=[Tag("plant", "Rose / hip & haw")]))
    assert client.get("/kb/tag/plant/Rose%20%2F%20hip%20%26%20haw").status_code == 200


def test_unknown_tag_and_note(client, notes, db):
    add_samples(notes, db)
    assert client.get("/kb/tag/plant/Dandelion").status_code == 404
    assert client.get("/kb/note/999").status_code == 404
    assert client.get("/kb/note/999/edit").status_code == 404


def test_new_note_form(client):
    page = client.get("/kb/new").text
    assert 'value="other" checked' in page and 'value="own"' in page
    assert '<option value="Herbalism"' in page and '<option value="Woodwork"' in page
    assert 'data-kind="plant"' in page and 'data-kind="condition"' in page
    assert 'data-tags="[]"' in page and 'data-note-id=""' in page


def test_edit_forms(client, notes, db, config):
    n = add_samples(notes, db)
    page = client.get(f"/kb/note/{n['other'].id}/edit").text
    assert 'value="other" checked' in page and 'value="Spring foraging walk"' in page
    assert 'value="https://example.org/walk"' in page
    assert '<option value="Herbalism" selected>' in page
    assert 'data-tags="[{&#34;kind&#34;: &#34;plant&#34;, &#34;name&#34;: &#34;Nettle&#34;}]"' in page

    page = client.get(f"/kb/note/{n['book'].id}/edit").text
    assert 'name="source_type" value="book"' in page and 'id="f-topic"' not in page  # from the book
    assert "Culpeper, p. 2" in page

    # A tag kind since removed from the config still gets a field, so it isn't lost on saving.
    config.tag_kinds.remove("condition")
    page = client.get(f"/kb/note/{n['book'].id}/edit").text
    assert 'data-kind="condition"' in page


def test_edit_form_keeps_a_topic_no_longer_in_the_library(client, notes):
    note = notes.create(Note(source_type="own", topic="Old topic", title="Kept", body="x"))
    page = client.get(f"/kb/note/{note.id}/edit").text
    assert '<option value="Old topic" selected>Old topic (no longer in the library)</option>' in page


def test_viewer_panel_links_scripts(client, db):
    page = client.get(f"/view/{file_row(db, 'Culpeper')['id']}/1").text
    assert "/static/kb.js" in page and "/static/kb_tags.js" not in page
