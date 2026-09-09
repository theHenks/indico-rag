"""Indico JSON to agenda model."""
from __future__ import annotations

import re
from datetime import date as Date
from datetime import datetime

from indico_rag.model import Attachment, Day, Event, Session, Talk

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_UNSCHEDULED = "Unscheduled"


def slugify(text: str) -> str:
    return _SLUG_RE.sub("-", text.lower()).strip("-") or "untitled"


def _timestamp(block: dict | None) -> datetime | None:
    if not block:
        return None
    raw_date, raw_time = block.get("date"), block.get("time", "00:00:00")
    if not raw_date:
        return None
    try:
        return datetime.fromisoformat(f"{raw_date}T{raw_time}")
    except ValueError:
        return None


def _surname(person: dict) -> str:
    """Indico spells this familyName in timetable entries and last_name in exports."""
    for key in ("familyName", "last_name", "lastName"):
        value = person.get(key)
        if value:
            return str(value).strip()
    return ""


def _person_name(person: dict) -> str:
    first = person.get("firstName") or person.get("first_name") or ""
    last = _surname(person)
    if first or last:
        return f"{first} {last}".strip()
    explicit = person.get("name") or person.get("fullName") or ""
    return str(explicit).strip()


def _attachment(raw: dict, folder_title: str) -> Attachment:
    kind = "link" if raw.get("type") == "link" or raw.get("link_url") else "file"
    url = raw.get("download_url") or raw.get("link_url") or raw.get("url") or ""
    filename = raw.get("filename") or raw.get("title") or url.rsplit("/", 1)[-1] or "attachment"
    modified = raw.get("modified_dt")
    uploaded = None
    if isinstance(modified, str):
        try:
            uploaded = datetime.fromisoformat(modified)
        except ValueError:
            uploaded = None
    return Attachment(
        id=str(raw.get("id", "")),
        filename=filename,
        kind=kind,
        url=url,
        size=raw.get("size"),
        content_type=raw.get("content_type"),
        uploaded=uploaded,
        folder_title=folder_title or "",
    )


def attachments_by_contribution(event_export: dict) -> dict[str, list[Attachment]]:
    """Map contribution id to its attachments, from an /export/event contributions payload."""
    found: dict[str, list[Attachment]] = {}
    for result in event_export.get("results", []):
        for contribution in result.get("contributions", []):
            # db_id is the identifier the timetable calls contributionId; the
            # export's own `id` is a friendly number that matches nothing.
            key = str(contribution.get("db_id") or contribution.get("id", ""))
            items: list[Attachment] = []
            for folder in contribution.get("folders") or []:
                folder_title = folder.get("title") or ""
                for raw in folder.get("attachments") or []:
                    items.append(_attachment(raw, folder_title))
            if items:
                found[key] = items
    return found


def _talk(entry: dict, session_title: str, synthetic: bool,
          attachments: dict[str, list[Attachment]] | None) -> Talk:
    contribution_id = str(entry.get("contributionId") or entry.get("db_id") or entry.get("id", ""))
    presenters = entry.get("presenters") or entry.get("speakers") or []

    # Inline timetable attachments are {"files": [...], "folders": [...]}, and
    # carry only a title and a site-relative URL.
    inline: list[Attachment] = []
    block = entry.get("attachments") or {}
    if isinstance(block, dict):
        for raw in block.get("files", []) or []:
            inline.append(_attachment(raw, ""))
        for folder in block.get("folders", []) or []:
            for raw in folder.get("attachments", []) or []:
                inline.append(_attachment(raw, folder.get("title") or ""))

    # The export wins: it carries size, content type and upload time, which
    # ranking and the manifest both need.
    resolved = (attachments or {}).get(contribution_id) or inline
    return Talk(
        id=str(entry.get("id", "")),
        contribution_id=contribution_id,
        title=entry.get("title", "").strip() or "Untitled",
        start=_timestamp(entry.get("startDate")),
        end=_timestamp(entry.get("endDate")),
        abstract=entry.get("description", "") or "",
        speakers=[_person_name(p) for p in presenters if _person_name(p)],
        speaker_surnames=[_surname(p) for p in presenters if _surname(p)],
        affiliations=[p.get("affiliation", "") for p in presenters if p.get("affiliation")],
        track=entry.get("track", "") or "",
        contribution_type=entry.get("type", "") or "",
        url=entry.get("url", "") or "",
        session_title=session_title,
        session_is_synthetic=synthetic,
        attachments=list(resolved),
    )


def parse_timetable(timetable: dict, *, host: str, event_id: str, event_title: str,
                    attachments: dict[str, list[Attachment]] | None = None) -> Event:
    """Build an Event from an /export/timetable payload.

    Break entries are dropped. Contributions scheduled outside any session are
    collected into a synthetic session so the tree always has this level.
    """
    event = Event(id=event_id, host=host.rstrip("/"), title=event_title,
                  slug=slugify(event_title), days=[])
    per_event = timetable.get("results", {}).get(str(event_id), {})

    for day_key in sorted(per_event):
        entries = per_event[day_key]
        try:
            day_date = Date.fromisoformat(f"{day_key[:4]}-{day_key[4:6]}-{day_key[6:8]}")
        except ValueError:
            continue

        sessions: dict[str, Session] = {}
        loose: list[Talk] = []

        for entry in entries.values():
            kind = entry.get("entryType")
            if kind == "Session":
                title = entry.get("title", "Session").strip() or "Session"
                session = Session(
                    id=str(entry.get("sessionId") or entry.get("id", "")),
                    title=title,
                    slug=entry.get("sessionSlug") or slugify(title),
                )
                for child in (entry.get("entries") or {}).values():
                    if child.get("entryType") != "Contribution":
                        continue
                    session.talks.append(_talk(child, title, False, attachments))
                if session.talks:
                    # A session can hold several slots in one day, a morning
                    # and an afternoon block sharing one session id. Their
                    # talks belong together under that session rather than one
                    # slot replacing the other and taking its talks with it.
                    key = session.id or session.slug
                    existing = sessions.get(key)
                    if existing is None:
                        sessions[key] = session
                    else:
                        existing.talks.extend(session.talks)
            elif kind == "Contribution":
                loose.append(_talk(entry, _UNSCHEDULED, True, attachments))

        # Order talks first, then order sessions by their earliest talk: Indico's
        # `entries` map has no guaranteed order, so a session's first-listed talk
        # is not necessarily its earliest.
        for session in sessions.values():
            session.talks.sort(key=lambda t: (t.start or datetime.min))

        ordered = sorted(
            sessions.values(),
            key=lambda s: min((t.start or datetime.min) for t in s.talks),
        )
        if loose:
            loose.sort(key=lambda t: (t.start or datetime.min))
            ordered.append(
                Session(id=f"synthetic-{day_key}", title=_UNSCHEDULED,
                        slug=slugify(_UNSCHEDULED), talks=loose, synthetic=True)
            )

        if ordered:
            event.days.append(Day(date=day_date, sessions=ordered))

    return event
