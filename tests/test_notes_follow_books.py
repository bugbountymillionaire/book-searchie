"""Book notes stay attached to their books as the library changes (KB step 2)."""

import shutil

import pytest

from book_searchie.indexer import run_index
from book_searchie.notes import Note, NotesStore, Tag


@pytest.fixture
def notes(config):
    with NotesStore.open(config.notes_path) as store:
        yield store


def save_book_note(notes, db, rel, seq=0, **kw):
    """A note on a book as the viewer would save it, from what's in the index."""
    row = db.conn.execute("SELECT path, topic, title FROM files WHERE rel = ?", (rel,)).fetchone()
    values = dict(
        source_type="book", topic=row["topic"], path=row["path"], seq=seq, label=str(seq + 1),
        book_title=row["title"], quote="Dovetail joints are strong.",
    )
    values.update(kw)
    return notes.create(Note(**values))


def other_and_own(notes):
    return [
        notes.create(Note(source_type="other", topic="Woodwork", title="Class", source_ref="Evening class",
                          body="Mark the waste side.", tags=[Tag("subject", "Dovetails")])),
        notes.create(Note(source_type="own", topic="Woodwork", title="Mine", body="Sharp saw, light grip.")),
    ]


def test_moved_book_takes_its_notes(config, db, library, notes):
    run_index(config, db, notes=notes)
    note = save_book_note(notes, db, "Woodwork/Joinery.pdf")
    (library / "Gardening" / "Tools").mkdir()
    shutil.move(library / "Woodwork" / "Joinery.pdf", library / "Gardening" / "Tools" / "Joinery.pdf")

    report = run_index(config, db, notes=notes)
    assert report.moved == 1 and report.notes_missing == 0
    moved = notes.get(note.id)
    assert moved.path == str(library / "Gardening" / "Tools" / "Joinery.pdf")
    assert (moved.topic, moved.book_title, moved.seq, moved.source_missing) == ("Gardening", "Joinery", 0, False)
    assert moved.updated == note.updated  # the note itself wasn't edited
    assert notes.for_page(moved.path, 0)[0].id == note.id


def test_renamed_book_takes_its_notes(config, db, library, notes):
    run_index(config, db, notes=notes)
    note = save_book_note(notes, db, "Woodwork/Joinery.pdf")
    (library / "Woodwork" / "Joinery.pdf").rename(library / "Woodwork" / "Joints and Joinery.pdf")
    run_index(config, db, notes=notes)
    assert (notes.get(note.id).book_title, notes.get(note.id).source_missing) == ("Joints and Joinery", False)


def test_moves_are_followed_in_a_full_rebuild_too(config, db, library, notes):
    run_index(config, db, notes=notes)
    note = save_book_note(notes, db, "Woodwork/Joinery.pdf")
    shutil.move(library / "Woodwork" / "Joinery.pdf", library / "Gardening" / "Joinery.pdf")
    report = run_index(config, db, full=True, notes=notes)
    assert report.notes_missing == 0
    assert (notes.get(note.id).topic, notes.get(note.id).source_missing) == ("Gardening", False)


def test_deleted_book_keeps_its_notes_marked_missing(config, db, library, notes):
    run_index(config, db, notes=notes)
    first = save_book_note(notes, db, "Woodwork/Joinery.pdf")
    second = save_book_note(notes, db, "Woodwork/Joinery.pdf", seq=0, quote="Another line.")
    keep = save_book_note(notes, db, "Loose.pdf", quote="Compost.")
    (library / "Woodwork" / "Joinery.pdf").unlink()

    report = run_index(config, db, notes=notes)
    assert report.removed == 1 and report.notes_missing == 2
    assert notes.count() == 3
    assert [notes.get(n.id).source_missing for n in (first, second, keep)] == [True, True, False]
    assert notes.get(first.id).path == first.path  # kept, in case the book comes back
    assert [h.note.id for h in notes.search("another")] == [second.id]  # still searchable

    # The book comes back: its notes are attached again.
    from .conftest import make_pdf

    make_pdf(library / "Woodwork" / "Joinery.pdf", ["Dovetail joints are strong."])
    report = run_index(config, db, notes=notes)
    assert report.notes_missing == 0
    assert notes.get(first.id).source_missing is False


def test_excluded_book_counts_as_missing(config, db, library, notes):
    run_index(config, db, notes=notes)
    note = save_book_note(notes, db, "Woodwork/Joinery.pdf")
    shutil.move(library / "Woodwork", library / "_Woodwork")  # "_" folders aren't searched
    assert run_index(config, db, notes=notes).notes_missing == 1
    assert notes.get(note.id).source_missing is True


def test_unplugged_drive_doesnt_mark_notes_missing(config, db, library, notes, tmp_path):
    from .conftest import make_pdf

    other = tmp_path / "Other"
    make_pdf(other / "Turning" / "Lathe.pdf", ["Bowl gouge."])
    config.roots.append(other)
    run_index(config, db, notes=notes)
    note = save_book_note(notes, db, "Turning/Lathe.pdf", quote="Bowl gouge.")
    shutil.move(other, tmp_path / "Unplugged")

    report = run_index(config, db, notes=notes)
    assert report.missing_roots == [str(other)] and report.notes_missing == 0
    assert notes.get(note.id).source_missing is False


def test_other_and_own_notes_are_untouched(config, db, library, notes):
    run_index(config, db, notes=notes)
    before = other_and_own(notes)
    shutil.move(library / "Woodwork" / "Joinery.pdf", library / "Gardening" / "Joinery.pdf")
    (library / "Loose.pdf").unlink()
    run_index(config, db, notes=notes)
    run_index(config, db, full=True, notes=notes)
    assert [notes.get(n.id) for n in before] == before


def test_index_uses_the_configured_notes_file(config, db, library):
    # Without notes=..., indexing opens the notes file from the config, if there is one.
    run_index(config, db)
    assert not config.notes_path.exists()  # indexing never creates it

    with NotesStore.open(config.notes_path) as notes:
        note = save_book_note(notes, db, "Woodwork/Joinery.pdf")
    shutil.move(library / "Woodwork" / "Joinery.pdf", library / "Gardening" / "Joinery.pdf")
    run_index(config, db)
    with NotesStore.open(config.notes_path) as notes:
        assert notes.get(note.id).topic == "Gardening"
