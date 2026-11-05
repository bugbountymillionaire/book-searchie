"""The KB pages (KB step 5)."""

import pytest
from fastapi.testclient import TestClient

from book_searchie.indexer import run_index
from book_searchie.notes import Note, NotesStore, Tag
from book_searchie.web.app import create_app


@pytest.fixture
def client(config, db):
    run_index(config, db)
    config.tag_kinds = ["plant", "condition"]
    return TestClient(create_app(config, allowed_hosts=["testserver"]))


@pytest.fixture
def notes(config):
    with NotesStore.open(config.notes_path) as store:
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
    assert page.status_code == 200 and "Your knowledge base is empty" in page.text
    assert 'href="/kb/new"' in page.text and "Save to KB" in page.text
    assert not config.notes_path.exists()  # looking doesn't create the notes file


def test_kb_home_lists_tags_and_recent_notes(client, notes, db):
    n = add_samples(notes, db)
    page = client.get("/kb").text
    assert "4 notes in your knowledge base" in page
    assert '<a class="chip" href="/kb/tag/plant/Nettle">Nettle<span>2</span></a>' in page
    assert '<a class="chip" href="/kb/tag/condition/Hot%20flushes">Hot flushes<span>1</span></a>' in page
    assert page.index(f'id="note-{n["own"].id}"') < page.index(f'id="note-{n["book"].id}"')  # newest first
    assert 'aria-current="page">KB</a>' in page


def test_note_cards_show_their_source(client, notes, db):
    n = add_samples(notes, db)
    culpeper = file_row(db, "Culpeper")["id"]
    herbal = file_row(db, "Modern Herbal")["id"]
    page = client.get("/kb").text
    assert '<span class="source-badge book">Book</span>' in page
    assert f'<a href="/view/{culpeper}/1">Culpeper, p. 2</a>' in page
    assert f'<a href="/view/{herbal}/1">Modern Herbal, Leaves</a>' in page  # EPUB: the chapter, not "p."
    assert '<span class="source-badge other">Other source</span>' in page
    assert 'href="https://example.org/walk" rel="noopener noreferrer" target="_blank">Spring foraging walk ↗</a>' in page
    assert '<span class="source-badge own">Own knowledge</span>' in page
    # The body is formatted, and anything else in it is escaped.
    assert "<p>Pick the <strong>top</strong> leaves.</p><ul><li>gloves</li><li>scissors</li></ul>" in page
    assert "Dry-fit &lt;everything&gt; first." in page
    assert f'href="/kb/note/{n["own"].id}/edit"' in page


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
    assert "1 note is from a book that's no longer in the library" in client.get("/kb").text


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
    page = client.get("/kb").text
    assert 'href="/kb/tag/plant/Rose%20%2F%20hip%20%26%20haw"' in page
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
    assert page.index("/static/kb_tags.js") < page.index("/static/kb.js")
