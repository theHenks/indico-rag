import shutil
import subprocess
import zipfile
from pathlib import Path

import pypdfium2
import pytest

from indico_rag.pipeline.extract import extract_text, sidecar_path, write_sidecar

_SOFFICE = shutil.which("soffice") or (
    "/Applications/LibreOffice.app/Contents/MacOS/soffice"
    if Path("/Applications/LibreOffice.app/Contents/MacOS/soffice").exists()
    else None
)

_CONTENT_XML_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<office:document-content
  xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
  xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0"
  xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"
  xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"
  xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0"
  xmlns:presentation="urn:oasis:names:tc:opendocument:xmlns:presentation:1.0"
  office:version="1.2">
  <office:automatic-styles>
    <style:style style:name="dp1" style:family="drawing-page"/>
  </office:automatic-styles>
  <office:body>
    <office:presentation>
      {pages}
    </office:presentation>
  </office:body>
</office:document-content>
"""

_PAGE_TEMPLATE = """
      <draw:page draw:name="{name}" draw:style-name="dp1" draw:master-page-name="Default">
        <draw:frame svg:width="20cm" svg:height="4cm" svg:x="1cm" svg:y="1cm">
          <draw:text-box><text:p>{text}</text:p></draw:text-box>
        </draw:frame>
      </draw:page>
"""

_STYLES_XML = """<?xml version="1.0" encoding="UTF-8"?>
<office:document-styles
  xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
  xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0"
  xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"
  xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0"
  office:version="1.2">
  <office:master-styles>
    <style:master-page style:name="Default" style:page-layout-name="PM1"/>
  </office:master-styles>
  <office:automatic-styles>
    <style:page-layout style:name="PM1">
      <style:page-layout-properties svg:width="28cm" svg:height="21cm"/>
    </style:page-layout>
  </office:automatic-styles>
</office:document-styles>
"""

_MANIFEST_XML = """<?xml version="1.0" encoding="UTF-8"?>
<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" manifest:version="1.2">
  <manifest:file-entry manifest:full-path="/" manifest:version="1.2" manifest:media-type="application/vnd.oasis.opendocument.presentation"/>
  <manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/>
  <manifest:file-entry manifest:full-path="styles.xml" manifest:media-type="text/xml"/>
</manifest:manifest>
"""


def _write_fixture_odp(path: Path, page_texts: list[str]) -> None:
    """Write a genuine ODP package with one slide per entry in `page_texts`.

    Built with the standard-library `zipfile` module, not by driving
    LibreOffice, so producing the input fixture needs no extra dependency.
    """
    pages = "".join(
        _PAGE_TEMPLATE.format(name=f"page{i}", text=text)
        for i, text in enumerate(page_texts)
    )
    content_xml = _CONTENT_XML_TEMPLATE.format(pages=pages)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "mimetype",
            "application/vnd.oasis.opendocument.presentation",
            compress_type=zipfile.ZIP_STORED,
        )
        archive.writestr("META-INF/manifest.xml", _MANIFEST_XML)
        archive.writestr("content.xml", content_xml)
        archive.writestr("styles.xml", _STYLES_XML)


def _convert_to_pdf(odp_path: Path) -> Path:
    subprocess.run(
        ["soffice", "--headless", "--convert-to", "pdf", "--outdir", str(odp_path.parent), str(odp_path)],
        check=True,
        timeout=60,
        capture_output=True,
    )
    return odp_path.with_suffix(".pdf")


@pytest.fixture
def one_page_pdf(tmp_path) -> Path:
    pdf = pypdfium2.PdfDocument.new()
    pdf.new_page(200, 200)
    path = tmp_path / "slide.pdf"
    pdf.save(str(path))
    pdf.close()
    return path


@pytest.fixture
def image_only_pdf(tmp_path) -> Path:
    """A PDF whose only page content is a solid-fill image, no text objects."""
    pdf = pypdfium2.PdfDocument.new()
    page = pdf.new_page(200, 200)
    bitmap = pypdfium2.PdfBitmap.new_native(50, 50, pypdfium2.raw.FPDFBitmap_BGR)
    for i in range(len(bitmap.buffer)):
        bitmap.buffer[i] = 128
    image = pypdfium2.PdfImage.new(pdf)
    image.set_bitmap(bitmap)
    image.set_matrix(pypdfium2.PdfMatrix().scale(100, 100).translate(20, 20))
    page.insert_obj(image)
    page.gen_content()
    path = tmp_path / "slide.pdf"
    pdf.save(str(path))
    bitmap.close()
    page.close()
    pdf.close()
    return path


def test_sidecar_path_sits_beside_the_pdf(tmp_path):
    assert sidecar_path(tmp_path / "a" / "b.pdf") == tmp_path / "a" / "b.txt"


def test_extracting_an_empty_page_yields_no_text_without_raising(one_page_pdf):
    assert extract_text(one_page_pdf).strip() == ""


def test_write_sidecar_creates_the_file_and_reports_the_length(one_page_pdf):
    path, length = write_sidecar(one_page_pdf)
    assert path.exists()
    assert length == len(path.read_text())


def test_a_corrupt_pdf_raises_pdfium_error(tmp_path):
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"not a pdf at all")
    with pytest.raises(pypdfium2.PdfiumError):
        extract_text(broken)


def test_an_image_only_pdf_yields_little_or_no_text_without_raising(image_only_pdf):
    text = extract_text(image_only_pdf)
    assert isinstance(text, str)
    assert text.strip() == ""


@pytest.mark.slow
@pytest.mark.skipif(_SOFFICE is None, reason="LibreOffice not installed")
def test_a_pdf_with_real_text_round_trips(tmp_path):
    odp = tmp_path / "deck.odp"
    _write_fixture_odp(odp, ["Neutrino oscillation results"])

    pdf = _convert_to_pdf(odp)

    assert "Neutrino oscillation results" in extract_text(pdf)


@pytest.mark.slow
@pytest.mark.skipif(_SOFFICE is None, reason="LibreOffice not installed")
def test_multi_page_text_is_returned_in_page_order(tmp_path):
    odp = tmp_path / "deck.odp"
    _write_fixture_odp(odp, ["ALPHA MARKER PAGE ONE", "BETA MARKER PAGE TWO"])

    pdf = _convert_to_pdf(odp)
    text = extract_text(pdf)

    assert text.index("ALPHA MARKER PAGE ONE") < text.index("BETA MARKER PAGE TWO")


@pytest.mark.slow
@pytest.mark.skipif(_SOFFICE is None, reason="LibreOffice not installed")
def test_write_sidecar_character_count_matches_for_a_real_document(tmp_path):
    odp = tmp_path / "deck.odp"
    _write_fixture_odp(odp, ["ALPHA MARKER PAGE ONE", "BETA MARKER PAGE TWO"])

    pdf = _convert_to_pdf(odp)
    path, length = write_sidecar(pdf)

    assert length > 0
    assert length == len(path.read_text())
