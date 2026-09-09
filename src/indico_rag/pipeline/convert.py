"""Render non-PDF slide files to PDF.

LibreOffice handles the Office and OpenDocument formats; Keynote files are
converted by driving Keynote.app, which is the only program that reads them.
"""
from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path

SOFFICE_FORMATS = {"pptx", "ppt", "odp", "doc", "docx", "odt"}
KEYNOTE_FORMATS = {"key"}

_MAC_SOFFICE = Path("/Applications/LibreOffice.app/Contents/MacOS/soffice")


class ConversionError(Exception):
    """A file could not be rendered to PDF."""


class ConverterMissing(ConversionError):
    """The external program needed for this format is not installed."""


def needs_conversion(suffix: str) -> bool:
    return suffix.lower() != "pdf"


def find_soffice() -> str | None:
    found = shutil.which("soffice") or shutil.which("libreoffice")
    if found:
        return found
    return str(_MAC_SOFFICE) if _MAC_SOFFICE.exists() else None


def _profile_dir(profile_index: int) -> Path:
    path = Path(tempfile.gettempdir()) / "indico-rag" / f"lo-profile-{profile_index}"
    path.mkdir(parents=True, exist_ok=True)
    return path


async def _run(argv: list[str], timeout: float) -> tuple[int, bytes]:
    process = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        process.kill()
        raise ConversionError(f"converter timed out after {timeout}s") from exc
    return process.returncode or 0, stdout or b""


async def _convert_with_soffice(source: Path, profile_index: int, timeout: float) -> Path:
    soffice = find_soffice()
    if soffice is None:
        raise ConverterMissing(
            "LibreOffice is required for this format; install it with "
            "`brew install --cask libreoffice`"
        )
    argv = [
        soffice,
        f"-env:UserInstallation=file://{_profile_dir(profile_index)}",
        "--headless",
        "--norestore",
        "--convert-to",
        "pdf",
        "--outdir",
        str(source.parent),
        str(source),
    ]
    code, output = await _run(argv, timeout)
    produced = source.with_suffix(".pdf")
    if not produced.exists() or produced.stat().st_size == 0:
        raise ConversionError(
            f"LibreOffice exited {code} without producing a PDF: "
            f"{output.decode(errors='replace')[:400]}"
        )
    return produced


async def _convert_with_keynote(source: Path, timeout: float) -> Path:
    produced = source.with_suffix(".pdf")
    script = (
        'on run argv\n'
        '  set src to POSIX file (item 1 of argv)\n'
        '  set dst to POSIX file (item 2 of argv)\n'
        '  tell application "Keynote"\n'
        '    set doc to open src\n'
        '    export doc to dst as PDF\n'
        '    close doc saving no\n'
        '  end tell\n'
        'end run'
    )
    code, output = await _run(
        ["osascript", "-e", script, str(source), str(produced)], timeout
    )
    if not produced.exists() or produced.stat().st_size == 0:
        raise ConversionError(
            f"Keynote exited {code} without producing a PDF: "
            f"{output.decode(errors='replace')[:400]}"
        )
    return produced


async def convert_to_pdf(source: Path, *, profile_index: int = 0, timeout: float = 120.0) -> Path:
    """Render `source` to a PDF beside it and return the PDF's path."""
    suffix = source.suffix.lstrip(".").lower()
    if suffix == "pdf":
        return source
    if suffix in SOFFICE_FORMATS:
        return await _convert_with_soffice(source, profile_index, timeout)
    if suffix in KEYNOTE_FORMATS:
        return await _convert_with_keynote(source, timeout)
    raise ConversionError(f"no converter for .{suffix}")
