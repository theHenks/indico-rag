"""Destination paths for downloaded talks."""
from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from indico_rag.model import Day, Event, Session, Talk

_ILLEGAL = re.compile(r"[\\/:*?\"<>|\x00-\x1f]+")
_SEPARATORS = re.compile(r"[\s_]+")
_RUNS = re.compile(r"-{2,}")


def sanitize(text: str, *, max_length: int = 80) -> str:
    """Make one path component out of arbitrary text, preserving legal unicode.

    `max_length` bounds both the character count and the UTF-8 encoded byte
    count of the result. A character-only cap is not enough: 80 characters of
    CJK or other multi-byte text can still encode to well over 255 bytes,
    which is the component-length limit most filesystems enforce.
    """
    normalized = unicodedata.normalize("NFC", text).strip()
    cleaned = _ILLEGAL.sub("-", normalized)
    cleaned = _SEPARATORS.sub("-", cleaned)
    cleaned = _RUNS.sub("-", cleaned).strip("-. ")
    cleaned = cleaned[:max_length].strip("-. ")
    encoded = cleaned.encode("utf-8")
    if len(encoded) > max_length:
        cleaned = encoded[:max_length].decode("utf-8", errors="ignore").strip("-. ")
    return cleaned or "untitled"


def speaker_component(talk: Talk) -> str:
    """The first speaker's surname.

    Uses the surname Indico reported rather than the last whitespace-separated
    word of the full name, which would turn "Colomer Molla" into "Molla".
    """
    if talk.speaker_surnames:
        return sanitize(talk.speaker_surnames[0], max_length=40)
    if talk.speakers:
        return sanitize(talk.speakers[0].split()[-1], max_length=40)
    return ""


def event_dir(root: Path, event: Event) -> Path:
    return root / f"{event.id}_{sanitize(event.slug or event.title)}"


def destination(root: Path, event: Event, day: Day, session: Session, talk: Talk, *,
                suffix: str = "pdf") -> Path:
    clock = talk.start.strftime("%H%M") if talk.start else "0000"
    parts = [clock, speaker_component(talk), sanitize(talk.title)]
    name = "_".join(part for part in parts if part)
    return event_dir(root, event) / day.date.isoformat() / sanitize(session.title) / f"{name}.{suffix}"


def deduplicate(path: Path, taken: set[Path]) -> Path:
    """Return `path`, or the first free `<stem>-<n>` variant when it is already taken."""
    if path not in taken:
        return path
    counter = 2
    while True:
        candidate = path.with_name(f"{path.stem}-{counter}{path.suffix}")
        if candidate not in taken:
            return candidate
        counter += 1
