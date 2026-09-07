import asyncio
import zipfile
from pathlib import Path

import pypdfium2 as pdfium
import pytest

from indico_rag.pipeline import convert
from indico_rag.pipeline.convert import (
    ConversionError,
    ConverterMissing,
    convert_to_pdf,
    needs_conversion,
)

_SOFFICE_INSTALLED = convert.find_soffice() is not None

_CONTENT_XML = """<?xml version="1.0" encoding="UTF-8"?>
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
      <draw:page draw:name="page1" draw:style-name="dp1" draw:master-page-name="Default">
        <draw:frame svg:width="20cm" svg:height="4cm" svg:x="1cm" svg:y="1cm">
          <draw:text-box>
            <text:p>Hello from a fixture ODP</text:p>
          </draw:text-box>
        </draw:frame>
      </draw:page>
    </office:presentation>
  </office:body>
</office:document-content>
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


def _write_fixture_odp(path: Path) -> None:
    """Write a minimal but genuine single-slide ODP package to `path`.

    Built directly with the standard-library `zipfile` module rather than by
    driving LibreOffice, so no extra dependency and no LibreOffice invocation
    is needed just to produce the input fixture.
    """
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "mimetype",
            "application/vnd.oasis.opendocument.presentation",
            compress_type=zipfile.ZIP_STORED,
        )
        archive.writestr("META-INF/manifest.xml", _MANIFEST_XML)
        archive.writestr("content.xml", _CONTENT_XML)
        archive.writestr("styles.xml", _STYLES_XML)


def _page_count(pdf_path: Path) -> int:
    document = pdfium.PdfDocument(pdf_path)
    try:
        return len(document)
    finally:
        document.close()


def test_pdf_needs_no_conversion():
    assert needs_conversion("pdf") is False


def test_pptx_and_key_need_conversion():
    assert needs_conversion("pptx") is True
    assert needs_conversion("key") is True


async def test_soffice_is_invoked_with_a_private_profile(tmp_path, monkeypatch):
    source = tmp_path / "deck.pptx"
    source.write_bytes(b"stub")
    recorded: dict = {}

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            # LibreOffice writes <stem>.pdf beside the source.
            source.with_suffix(".pdf").write_bytes(b"%PDF-1.4 stub")
            return b"", b""

        def kill(self):  # pragma: no cover - not reached in this test
            pass

    async def fake_exec(*args, **kwargs):
        recorded["args"] = args
        return FakeProcess()

    monkeypatch.setattr(convert, "find_soffice", lambda: "/fake/soffice")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    result = await convert_to_pdf(source, profile_index=3)

    assert result == source.with_suffix(".pdf")
    profile_args = [a for a in recorded["args"] if str(a).startswith("-env:UserInstallation=")]
    assert len(profile_args) == 1, "each conversion must get its own LibreOffice profile"
    assert profile_args[0].endswith("lo-profile-3")
    assert "--headless" in recorded["args"]


async def test_keynote_is_invoked_with_source_and_destination_paths(tmp_path, monkeypatch):
    source = tmp_path / "deck.key"
    source.write_bytes(b"stub")
    recorded: dict = {}

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            source.with_suffix(".pdf").write_bytes(b"%PDF-1.4 stub")
            return b"", b""

        def kill(self):  # pragma: no cover - not reached in this test
            pass

    async def fake_exec(*args, **kwargs):
        recorded["args"] = args
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    result = await convert_to_pdf(source)

    assert result == source.with_suffix(".pdf")
    args = recorded["args"]
    assert args[0] == "osascript"
    assert args[1] == "-e"
    assert "export doc to dst as PDF" in args[2]
    assert args[-2:] == (str(source), str(source.with_suffix(".pdf")))


async def test_a_hanging_soffice_is_killed_and_reported(tmp_path, monkeypatch):
    source = tmp_path / "deck.pptx"
    source.write_bytes(b"stub")
    killed = {"count": 0}

    class HangingProcess:
        returncode = None

        async def communicate(self):
            await asyncio.sleep(10)
            return b"", b""

        def kill(self):
            killed["count"] += 1

    async def fake_exec(*args, **kwargs):
        return HangingProcess()

    monkeypatch.setattr(convert, "find_soffice", lambda: "/fake/soffice")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    with pytest.raises(ConversionError, match="timed out"):
        await convert_to_pdf(source, timeout=0.05)
    assert killed["count"] == 1


async def test_a_missing_converter_is_reported_clearly(tmp_path, monkeypatch):
    source = tmp_path / "deck.pptx"
    source.write_bytes(b"stub")
    monkeypatch.setattr(convert, "find_soffice", lambda: None)
    with pytest.raises(ConverterMissing, match="libreoffice"):
        await convert_to_pdf(source)


async def test_an_unsupported_suffix_is_rejected(tmp_path):
    source = tmp_path / "archive.zip"
    source.write_bytes(b"stub")
    with pytest.raises(ConversionError, match="no converter"):
        await convert_to_pdf(source)


@pytest.mark.slow
@pytest.mark.skipif(not _SOFFICE_INSTALLED, reason="LibreOffice not installed")
async def test_real_libreoffice_converts_an_odp_to_a_real_pdf(tmp_path):
    source = tmp_path / "deck.odp"
    _write_fixture_odp(source)

    result = await convert_to_pdf(source)

    assert result == source.with_suffix(".pdf")
    assert result.exists()
    assert result.read_bytes()[:4] == b"%PDF"
    assert _page_count(result) >= 1


@pytest.mark.slow
@pytest.mark.skipif(not _SOFFICE_INSTALLED, reason="LibreOffice not installed")
async def test_concurrent_conversions_with_separate_profiles_both_succeed(tmp_path):
    source_a = tmp_path / "a" / "deck.odp"
    source_b = tmp_path / "b" / "deck.odp"
    source_a.parent.mkdir()
    source_b.parent.mkdir()
    _write_fixture_odp(source_a)
    _write_fixture_odp(source_b)

    result_a, result_b = await asyncio.gather(
        convert_to_pdf(source_a, profile_index=0),
        convert_to_pdf(source_b, profile_index=1),
    )

    assert result_a.exists() and result_b.exists()
    assert _page_count(result_a) >= 1
    assert _page_count(result_b) >= 1


@pytest.mark.slow
@pytest.mark.skipif(not _SOFFICE_INSTALLED, reason="LibreOffice not installed")
async def test_concurrent_conversions_sharing_one_profile(tmp_path, capsys):
    """Demonstrate why `convert_to_pdf` gives each call its own profile.

    Two `soffice` processes launched against the *same* UserInstallation
    directory race: LibreOffice arbitrates access to a profile with a lock,
    and the loser attaches to the winner's running instance instead of
    performing its own conversion, then exits having produced nothing. This
    test does not assert a specific outcome — that arbitration is an
    implementation detail of LibreOffice and may differ across versions —
    it only records what actually happened for the report.
    """
    source_a = tmp_path / "a" / "deck.odp"
    source_b = tmp_path / "b" / "deck.odp"
    source_a.parent.mkdir()
    source_b.parent.mkdir()
    _write_fixture_odp(source_a)
    _write_fixture_odp(source_b)

    results = await asyncio.gather(
        convert_to_pdf(source_a, profile_index=9, timeout=60.0),
        convert_to_pdf(source_b, profile_index=9, timeout=60.0),
        return_exceptions=True,
    )

    outcomes = []
    for source, result in zip((source_a, source_b), results):
        if isinstance(result, BaseException):
            outcomes.append(f"{source.parent.name}: raised {result!r}")
        else:
            pdf_path = result
            if pdf_path.exists() and _page_count(pdf_path) >= 1:
                outcomes.append(f"{source.parent.name}: succeeded ({pdf_path})")
            else:
                outcomes.append(f"{source.parent.name}: returned {pdf_path} but it has no pages")

    print("shared-profile outcome:", "; ".join(outcomes))
