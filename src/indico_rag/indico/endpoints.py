"""Indico URL construction and event-reference parsing."""
from __future__ import annotations

import re
from urllib.parse import urlparse

_EVENT_RE = re.compile(r"/event/(\d+)")


def parse_event_ref(text: str, default_host: str | None = None) -> tuple[str, str]:
    """Turn a pasted event URL or a bare event id into (host, event_id)."""
    text = text.strip()
    if text.isdigit():
        if not default_host:
            raise ValueError("a bare event id needs a host; paste the full event URL instead")
        return default_host.rstrip("/"), text

    parsed = urlparse(text)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"not an event URL or id: {text!r}")
    match = _EVENT_RE.search(parsed.path)
    if match is None:
        raise ValueError(f"no /event/<id> segment in {text!r}")
    return f"{parsed.scheme}://{parsed.netloc}", match.group(1)


def timetable_path(event_id: str) -> str:
    return f"/export/timetable/{event_id}.json"


def contributions_path(event_id: str) -> str:
    return f"/export/event/{event_id}.json"
