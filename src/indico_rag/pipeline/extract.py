"""Text sidecars for downloaded PDFs.

Uses pypdfium2, which is permissively licensed; PyMuPDF's AGPL licence would
propagate to this project.
"""
from __future__ import annotations

from pathlib import Path

import pypdfium2


def sidecar_path(pdf: Path) -> Path:
    return pdf.with_suffix(".txt")


def extract_text(pdf: Path) -> str:
    """Return the text of every page, joined by a blank line between pages.

    A slide deck made of images yields little or no text. That is a property
    of the input, not an error, and is reported by the caller as a character
    count.

    Each page and textpage handle is closed as soon as its text is read.
    pypdfium2 closes these native handles automatically once the wrapper
    object is garbage-collected, but that collection can be deferred to a
    cyclic-GC pass rather than happening immediately on the last reference
    going out of scope, letting hundreds of open handles accumulate while a
    large corpus is processed in one process. Closing explicitly bounds that
    to one page and one textpage at a time.
    """
    document = pypdfium2.PdfDocument(str(pdf))
    try:
        pages = []
        for index in range(len(document)):
            page = document[index]
            try:
                textpage = page.get_textpage()
                try:
                    pages.append(textpage.get_text_range())
                finally:
                    textpage.close()
            finally:
                page.close()
        return "\n\n".join(pages)
    finally:
        document.close()


def write_sidecar(pdf: Path) -> tuple[Path, int]:
    text = extract_text(pdf)
    path = sidecar_path(pdf)
    path.write_text(text)
    return path, len(text)
