import os
import shutil

import pytest

from book_searchie.indexer import run_index
from book_searchie.search import QueryError, prepare_query, search

from .conftest import make_pdf


def _titles(db, query, **kw):
    return {h.title for h in search(db, query, **kw)}


def test_index_then_search(config, db):
    report = run_index(config, db)
    assert report.added == 5
    hit = search(db, '"black cohosh"')[0]
    assert (hit.topic, hit.title, hit.label, hit.seq) == ("Herbalism", "Culpeper", "2", 1)
    assert "[black cohosh]" in hit.snippet
    epub_hit = search(db, "valerian")[0]
    assert (epub_hit.kind, epub_hit.label) == ("epub", "TOC Roots")


def test_excluded_folders_not_indexed(config, db):
    run_index(config, db)
    assert _titles(db, "dovetail") == {"Joinery"}
    assert _titles(db, "inbox") == set()


def test_query_syntax(config, db):
    run_index(config, db)
    assert _titles(db, "echinac*") == {"Culpeper"}
    assert _titles(db, "root OR dovetail") == {"Culpeper", "Modern Herbal", "Joinery"}
    assert _titles(db, "root NOT valerian") == {"Culpeper"}
    assert _titles(db, "root", topic="woodwork") == set()
    assert _titles(db, "broad beans", topic="Gardening") == {"Spring"}


def test_prepare_query():
    assert prepare_query('st-john\'s "black cohosh" AND root*') == '"st-john\'s" "black cohosh" AND root*'
    assert prepare_query('unclosed "quote') == "unclosed quote"
    with pytest.raises(QueryError):
        prepare_query("  AND ( ")


def test_bad_query_gives_friendly_error(config, db):
    run_index(config, db)
    with pytest.raises(QueryError):
        search(db, "root AND")


def test_incremental_reindex(config, db, library):
    run_index(config, db)
    report = run_index(config, db)
    assert (report.added, report.updated, report.removed, report.unchanged) == (0, 0, 0, 5)

    joinery = library / "Woodwork" / "Joinery.pdf"
    make_pdf(joinery, ["Mortise and tenon joints."])
    os.utime(joinery, (1, 1))  # make sure mtime differs even on coarse filesystems
    (library / "Gardening" / "Seasonal" / "Spring.pdf").unlink()
    make_pdf(library / "Woodwork" / "Lathe.pdf", ["Turning bowls."])

    report = run_index(config, db)
    assert (report.added, report.updated, report.removed, report.unchanged) == (1, 1, 1, 3)
    assert _titles(db, "dovetail") == set()
    assert _titles(db, "mortise") == {"Joinery"}
    assert _titles(db, "beans") == set()
    assert _titles(db, "bowls") == {"Lathe"}


def test_newly_excluded_files_are_removed(config, db, library):
    run_index(config, db)
    shutil.move(library / "Woodwork", library / "_Woodwork")
    run_index(config, db)
    assert _titles(db, "dovetail") == set()


def test_missing_root_keeps_index(config, db, tmp_path):
    run_index(config, db)
    config.roots = [tmp_path / "unplugged"]
    report = run_index(config, db)
    assert report.missing_roots and report.removed == 0
    assert _titles(db, "dovetail") == {"Joinery"}


def test_problem_files_are_reported(config, db, library):
    make_pdf(library / "Herbalism" / "Scanned.pdf", ["", ""])
    (library / "Herbalism" / "Broken.epub").write_bytes(b"junk")
    run_index(config, db)
    problems = {r["rel"]: r["status"] for r in db.problem_files()}
    assert problems == {"Herbalism/Scanned.pdf": "no_text", "Herbalism/Broken.epub": "error"}


def test_full_rebuild(config, db):
    run_index(config, db)
    report = run_index(config, db, full=True)
    assert report.added == 5


def _file(db, title):
    return db.conn.execute("SELECT * FROM files WHERE title = ?", (title,)).fetchone()


def test_moved_book_is_not_read_again(config, db, library):
    run_index(config, db)
    before = _file(db, "Joinery")
    (library / "Gardening" / "Tools").mkdir()
    shutil.move(library / "Woodwork" / "Joinery.pdf", library / "Gardening" / "Tools" / "Joinery.pdf")

    seen = []
    report = run_index(config, db, progress=seen.append)
    assert seen == [] and report.moved == 1  # recognised, not read again
    assert (report.added, report.updated, report.removed, report.unchanged) == (0, 0, 0, 4)
    after = _file(db, "Joinery")
    assert after["id"] == before["id"]  # links to its pages keep working
    assert (after["topic"], after["rel"]) == ("Gardening", "Gardening/Tools/Joinery.pdf")
    assert _titles(db, "dovetail", topic="Gardening") == {"Joinery"}


def test_renamed_book_gets_its_new_title(config, db, library):
    run_index(config, db)
    (library / "Woodwork" / "Joinery.pdf").rename(library / "Woodwork" / "Joints and Joinery.pdf")
    report = run_index(config, db)
    assert report.moved == 1 and _titles(db, "dovetail") == {"Joints and Joinery"}


def test_ambiguous_moves_are_read_again(config, db, library):
    # Two identical copies (same size and time) both move and get new names: which is which
    # can't be told, so both are read again rather than guessed.
    a = library / "Woodwork" / "Copy A.pdf"
    shutil.copy2(library / "Woodwork" / "Joinery.pdf", a)
    shutil.copy2(a, library / "Woodwork" / "Copy B.pdf")
    run_index(config, db)
    a.rename(library / "Woodwork" / "Copy C.pdf")
    (library / "Woodwork" / "Copy B.pdf").rename(library / "Woodwork" / "Copy D.pdf")
    report = run_index(config, db)
    assert report.moved == 0 and (report.added, report.removed) == (2, 2)

    # But when one of them keeps its name, that settles it.
    (library / "Woodwork" / "Copy C.pdf").rename(library / "Gardening" / "Copy C.pdf")
    report = run_index(config, db)
    assert report.moved == 1


def test_moved_book_rejoins_duplicate_rules(config, db, library):
    from .conftest import make_epub

    make_epub(library / "Gardening" / "Joinery.epub", [("Joints", "<p>Dovetail joints are strong.</p>")])
    run_index(config, db)
    assert _file(db, "Joinery")["status"] == "ok"  # PDF and EPUB in different folders: both searched
    shutil.move(library / "Woodwork" / "Joinery.pdf", library / "Gardening" / "Joinery.pdf")
    report = run_index(config, db)
    assert report.moved == 1
    kinds = {r["kind"]: r["status"] for r in db.conn.execute("SELECT kind, status FROM files WHERE title = 'Joinery'")}
    assert kinds == {"epub": "ok", "pdf": "duplicate"}  # now side by side, so only the EPUB is searched
