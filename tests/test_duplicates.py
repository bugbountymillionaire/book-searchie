"""The same book in several formats in one folder is only searched once."""

import os

import pytest

from book_searchie.config import ConfigError, _build
from book_searchie.indexer import run_index
from book_searchie.search import search

from .conftest import make_epub, make_pdf


@pytest.fixture
def pair(library):
    """Goats.pdf and goats.EPUB side by side (names differ only in case)."""
    folder = library / "Animals"
    pdf = make_pdf(folder / "Goats.pdf", ["Goats need a dry shelter."])
    epub = make_epub(folder / "goats.EPUB", [("Housing", "<p>Goats need a dry shelter.</p>")])
    return pdf, epub


def _status(db, path):
    return db.status_of(path)


def _kinds_found(db, query):
    return [h.kind for h in search(db, query)]


def test_preferred_copy_is_searched(config, db, pair):
    pdf, epub = pair
    report = run_index(config, db)
    assert _kinds_found(db, "shelter") == ["epub"]
    assert _status(db, pdf) == "duplicate" and _status(db, epub) == "ok"
    assert report.duplicates == 1
    assert db.stats()["books"] == 5 + 1  # the library fixture's books plus one goat book
    assert [r["rel"] for r in db.duplicate_files()] == ["Animals/Goats.pdf"]
    assert all(r["status"] != "duplicate" for r in db.problem_files())


def test_prefer_pdf(config, db, pair):
    config.prefer = "pdf"
    run_index(config, db)
    assert _kinds_found(db, "shelter") == ["pdf"]


def test_switching_preference_swaps_copies(config, db, pair):
    pdf, epub = pair
    run_index(config, db)
    config.prefer = "pdf"
    report = run_index(config, db)
    assert _kinds_found(db, "shelter") == ["pdf"]
    assert _status(db, epub) == "duplicate"
    assert report.updated == 2


def test_falls_back_when_preferred_copy_is_broken(config, db, pair):
    pdf, epub = pair
    epub.write_bytes(b"not really an epub")
    run_index(config, db)
    assert _kinds_found(db, "shelter") == ["pdf"]
    assert _status(db, epub) == "error"  # still listed, so you know the EPUB needs fixing

    # Once the EPUB is fixed, it takes over again.
    make_epub(epub, [("Housing", "<p>Goats need a dry shelter.</p>")])
    os.utime(epub, (1, 1))
    run_index(config, db)
    assert _kinds_found(db, "shelter") == ["epub"]
    assert _status(db, pdf) == "duplicate"


def test_other_copy_used_when_preferred_is_deleted(config, db, pair):
    pdf, epub = pair
    run_index(config, db)
    epub.unlink()
    report = run_index(config, db)
    assert _kinds_found(db, "shelter") == ["pdf"]
    assert report.removed == 1 and report.duplicates == 0


def test_unchanged_pair_is_not_reread(config, db, pair):
    run_index(config, db)
    seen = []
    report = run_index(config, db, progress=seen.append)
    assert seen == [] and report.added == report.updated == 0


def test_same_name_in_different_folders_are_both_kept(config, db, library):
    make_pdf(library / "Animals" / "Goats.pdf", ["Goats graze."])
    make_epub(library / "Farming" / "Goats.epub", [("Grazing", "<p>Goats graze.</p>")])
    run_index(config, db)
    assert sorted(_kinds_found(db, "graze")) == ["epub", "pdf"]


def test_prefer_setting_is_validated(tmp_path):
    assert _build({"library": {"roots": ["x"], "prefer": "PDF"}}, tmp_path, None).prefer == "pdf"
    with pytest.raises(ConfigError, match="prefer"):
        _build({"library": {"roots": ["x"], "prefer": "mobi"}}, tmp_path, None)
