import pytest

from book_searchie.extract import ExtractError, extract_epub, extract_pdf, normalize_text

from .conftest import make_epub, make_pdf


def test_normalize_text():
    assert normalize_text("ﬁne ﬂower") == "fine flower"
    assert normalize_text("medi-\ncine") == "medicine"
    assert normalize_text("Anglo-\nSaxon") == "Anglo-Saxon"
    assert normalize_text("soft­hyphen  and\n\nspaces here") == "softhyphen and spaces here"


def test_pdf_pages_and_labels(tmp_path):
    path = make_pdf(
        tmp_path / "b.pdf",
        ["Front matter. " * 5, "", "Chapter one text. " * 5],
        labels=[{"startpage": 0, "prefix": "", "style": "r", "firstpagenum": 1},
                {"startpage": 2, "prefix": "", "style": "D", "firstpagenum": 1}],
    )
    result = extract_pdf(path)
    assert result.units == 3
    assert [(s.seq, s.label) for s in result.sections] == [(0, "i"), (2, "1")]  # empty page skipped
    assert result.sections[1].text.startswith("Chapter one text. Chapter")
    assert not result.no_text


def test_pdf_without_text_is_flagged(tmp_path):
    result = extract_pdf(make_pdf(tmp_path / "scan.pdf", ["", "", ""]))
    assert result.no_text


def test_corrupt_pdf(tmp_path):
    path = tmp_path / "bad.pdf"
    path.write_bytes(b"not a pdf")
    with pytest.raises(ExtractError):
        extract_pdf(path)


def test_epub_chapters(tmp_path):
    path = make_epub(tmp_path / "b.epub", [("One", "<p>Alpha &amp; beta</p><script>x()</script>"), ("Two", "<p>Gamma</p>")])
    result = extract_epub(path)
    assert result.units == 2
    first = result.sections[0]
    assert first.label == "TOC One"  # nav title preferred over heading
    assert first.href == "OEBPS/text/ch1.xhtml"
    assert first.text == "One Alpha & beta"  # no <title>, <style> or <script> text


def test_epub_falls_back_to_heading(tmp_path):
    result = extract_epub(make_epub(tmp_path / "b.epub", [("Heading", "<p>x</p>")], nav=False))
    assert result.sections[0].label == "Heading"


def test_epub_with_drm(tmp_path):
    enc = (
        '<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container"'
        ' xmlns:enc="http://www.w3.org/2001/04/xmlenc#"><enc:EncryptedData>'
        '<enc:CipherData><enc:CipherReference URI="OEBPS/text/ch1.xhtml"/></enc:CipherData>'
        "</enc:EncryptedData></encryption>"
    )
    path = make_epub(tmp_path / "b.epub", [("One", "<p>x</p>")], extra={"META-INF/encryption.xml": enc})
    with pytest.raises(ExtractError, match="DRM"):
        extract_epub(path)


def test_not_an_epub(tmp_path):
    path = tmp_path / "bad.epub"
    path.write_bytes(b"nope")
    with pytest.raises(ExtractError):
        extract_epub(path)
