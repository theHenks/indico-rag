"""Pick exactly one attachment per talk.

Ranking runs at parse time so the interface can show the resulting filename on
every row and an override is a single field reassignment.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from indico_rag.model import Attachment, Event, Talk

FORMAT_ORDER: dict[str, int] = {
    "pdf": 0,
    "pptx": 1, "ppt": 1, "odp": 1,
    "key": 2,
}
LINK_ORDER = 3

_PREFERRED = re.compile(r"slide|talk|presentation|deck", re.I)
_DEPRIORITIZED = re.compile(r"poster|backup|paper|proceeding|abstract", re.I)
_GOOGLE_SLIDES = re.compile(r"docs\.google\.com/presentation/d/([A-Za-z0-9_-]+)")


def is_google_slides(url: str) -> bool:
    return _GOOGLE_SLIDES.search(url) is not None


def google_slides_pdf_url(url: str) -> str:
    match = _GOOGLE_SLIDES.search(url)
    if match is None:
        raise ValueError(f"not a Google Slides URL: {url!r}")
    return f"https://docs.google.com/presentation/d/{match.group(1)}/export/pdf"


def _format_rank(attachment: Attachment) -> int | None:
    if attachment.kind == "link":
        return LINK_ORDER if is_google_slides(attachment.url) else None
    return FORMAT_ORDER.get(attachment.suffix)


def _upload_key(uploaded: datetime | None) -> tuple[int, float]:
    """Sort key for upload recency; a missing upload time sorts last.

    Attachment.uploaded is timezone-aware for real Indico data but may be
    naive in synthetic data; a naive value is treated as UTC so the two
    never end up compared against each other by direct subtraction.
    """
    if uploaded is None:
        return (1, 0.0)
    aware = uploaded if uploaded.tzinfo is not None else uploaded.replace(tzinfo=timezone.utc)
    return (0, -aware.timestamp())


def rank(attachment: Attachment) -> tuple:
    """Sort key; lower is better. Only call on attachments with a usable format."""
    format_rank = _format_rank(attachment)
    assert format_rank is not None
    text = f"{attachment.folder_title} {attachment.filename}"
    name_rank = 1
    if _PREFERRED.search(text):
        name_rank = 0
    if _DEPRIORITIZED.search(text):
        name_rank = 2
    return (format_rank, _upload_key(attachment.uploaded), name_rank, attachment.filename)


def choose(talk: Talk) -> Attachment | None:
    usable = [a for a in talk.attachments if _format_rank(a) is not None]
    if not usable:
        return None
    return min(usable, key=rank)


def apply_ranking(event: Event) -> None:
    for talk in event.all_talks():
        talk.chosen = choose(talk)
