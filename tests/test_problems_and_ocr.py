import shutil

import pymupdf
import pytest

from book_searchie import ocr
from book_searchie.cli import main
from book_searchie.extract import DrmProtected, PasswordProtected, extract_epub, extract_pdf
from book_searchie.indexer import run_index
from book_searchie.search import search

from .conftest import make_epub, make_pdf


def _locked_pdf(path, handler: bytes | None = None):
    doc = pymupdf.open()
    doc.new_page().insert_text((50, 50), "Secret text")
    doc.save(path, encryption=pymupdf.PDF_ENCRYPT_RC4_128, user_pw="pw", owner_pw="owner")
    doc.close()
    if handler:
        # Swap the standard security handler for a DRM one, as a DRM'd PDF would have.
        data = path.read_bytes()
        assert b"/Standard" in data
        path.write_bytes(data.replace(b"/Standard", b"/" + handler))
    return path


def test_password_pdf(tmp_path):
    with pytest.raises(PasswordProtected):
        extract_pdf(_locked_pdf(tmp_path / "locked.pdf"))


def test_drm_pdf(tmp_path):
    with pytest.raises(DrmProtected, match="Adobe Digital Editions"):
        extract_pdf(_locked_pdf(tmp_path / "drm.pdf", b"EBX_HANDLER"))


def test_drm_epub_names_vendor(tmp_path):
    enc = (
        '<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container"'
        ' xmlns:enc="http://www.w3.org/2001/04/xmlenc#"><enc:EncryptedData><enc:CipherData>'
        '<enc:CipherReference URI="OEBPS/text/ch1.xhtml"/></enc:CipherData></enc:EncryptedData></encryption>'
    )
    path = make_epub(
        tmp_path / "b.epub", [("One", "<p>x</p>")],
        extra={"META-INF/encryption.xml": enc, "META-INF/rights.xml": "<rights/>"},
    )
    with pytest.raises(DrmProtected, match="Adobe"):
        extract_epub(path)


def test_statuses_recorded(config, db, library):
    _locked_pdf(library / "Herbalism" / "Locked.pdf")
    _locked_pdf(library / "Herbalism" / "Shop.pdf", b"EBX_HANDLER")
    make_pdf(library / "Herbalism" / "Scanned.pdf", ["", ""])
    run_index(config, db)
    statuses = {r["rel"]: r["status"] for r in db.problem_files()}
    assert statuses == {
        "Herbalism/Locked.pdf": "password",
        "Herbalism/Shop.pdf": "drm",
        "Herbalism/Scanned.pdf": "no_text",
    }


@pytest.fixture
def fake_ocr(monkeypatch):
    """Stand in for OCRmyPDF: the 'OCR'd copy' is a PDF with known text."""
    calls = []

    def ocr_pdf(src, dest, language, **kwargs):
        calls.append(src)
        dest.parent.mkdir(parents=True, exist_ok=True)
        make_pdf(dest, ["Hawthorn berries support the heart. " * 3, "Second page of recognised text. " * 3])

    monkeypatch.setattr(ocr, "check_available", lambda: None)
    monkeypatch.setattr(ocr, "ocr_pdf", ocr_pdf)
    return calls


def test_ocr_makes_scanned_book_searchable(config, db, library, fake_ocr):
    scanned = make_pdf(library / "Herbalism" / "Scanned.pdf", ["", ""])
    original = scanned.read_bytes()
    run_index(config, db)
    assert search(db, "hawthorn") == []

    rows = ocr.ocr_candidates(db)
    assert [r["rel"] for r in rows] == ["Herbalism/Scanned.pdf"]
    report = ocr.run_ocr(config, rows)
    assert report.done == ["Herbalism/Scanned.pdf"] and not report.failed
    assert scanned.read_bytes() == original  # the library is never modified

    index_report = run_index(config, db)
    assert index_report.updated == 1
    hit = search(db, "hawthorn")[0]
    assert (hit.title, hit.label) == ("Scanned", "1")
    assert db.problem_files() == [] and db.ocr_count() == 1
    assert ocr.ocr_candidates(db) == []

    # Renaming the book keeps its OCR copy.
    scanned.rename(library / "Herbalism" / "Renamed.pdf")
    run_index(config, db)
    assert search(db, "hawthorn")[0].title == "Renamed"

    # Deleting the cached copy puts the book back on the scanned list.
    shutil.rmtree(ocr.ocr_dir(config))
    run_index(config, db)
    assert search(db, "hawthorn") == []
    assert [r["rel"] for r in ocr.ocr_candidates(db)] == ["Herbalism/Renamed.pdf"]


def test_ocr_candidates_by_name(config, db, library):
    make_pdf(library / "Herbalism" / "Scanned.pdf", ["", ""])
    make_pdf(library / "Woodwork" / "Old Plans.pdf", [""])
    run_index(config, db)
    assert [r["rel"] for r in ocr.ocr_candidates(db, ["woodwork\\old plans.pdf"])] == ["Woodwork/Old Plans.pdf"]
    full = str(library / "Herbalism" / "Scanned.pdf")
    assert [r["rel"] for r in ocr.ocr_candidates(db, [full])] == ["Herbalism/Scanned.pdf"]
    with pytest.raises(ValueError, match="Joinery"):
        ocr.ocr_candidates(db, ["Woodwork/Joinery.pdf"])  # has text already


def test_ocr_command_without_ocrmypdf(config, db, library, tmp_path, monkeypatch, capsys):
    make_pdf(library / "Herbalism" / "Scanned.pdf", ["", ""])
    run_index(config, db)

    def missing():
        raise ocr.OcrUnavailable("OCRmyPDF isn't installed.\n\n" + ocr.INSTALL_HELP)

    monkeypatch.setattr(ocr, "check_available", missing)
    cfg = tmp_path / "book-searchie.toml"
    cfg.write_text(f'[library]\nroots = ["{library.as_posix()}"]\n[index]\npath = "index.db"\n', encoding="utf-8")
    assert main(["--config", str(cfg), "ocr"]) == 2
    err = capsys.readouterr().err
    assert "winget install UB-Mannheim.TesseractOCR" in err and "Ghostscript" not in err


def test_report_explains_fixes(config, db, library, tmp_path, capsys):
    make_pdf(library / "Herbalism" / "Scanned.pdf", ["", ""])
    _locked_pdf(library / "Herbalism" / "Shop.pdf", b"EBX_HANDLER")
    cfg = tmp_path / "book-searchie.toml"
    cfg.write_text(f'[library]\nroots = ["{library.as_posix()}"]\n[index]\npath = "index.db"\n', encoding="utf-8")
    assert main(["--config", str(cfg), "index", "-q"]) == 0
    out = capsys.readouterr().out
    assert "2 book(s) can't be searched: 1 scanned (fix: book-searchie ocr); 1 DRM-protected." in out
    assert main(["--config", str(cfg), "report"]) == 0
    out = capsys.readouterr().out
    assert "Scanned books" in out and "Herbalism/Scanned.pdf  (2 pages)" in out
    assert "Herbalism/Shop.pdf  (Adobe Digital Editions)" in out
    assert "DRM-free copy" in " ".join(out.split())
