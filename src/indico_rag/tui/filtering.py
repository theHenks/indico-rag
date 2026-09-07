"""Narrowing which talks are displayed.

Filtering changes what is shown and never what is selected; the two live in
separate modules so that separation is structural rather than a convention.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date as Date
from typing import Literal

from indico_rag.indico.ranking import FORMAT_ORDER
from indico_rag.model import Event, Talk

MaterialState = Literal["pdf", "convert", "none"]


class InvalidPattern(Exception):
    """The regular expression in the filter box does not compile."""


def material_state(talk: Talk) -> MaterialState:
    if talk.chosen is None:
        return "none"
    if talk.chosen.kind == "file" and talk.chosen.suffix == "pdf":
        return "pdf"
    return "convert"


@dataclass
class FilterSpec:
    text: str = ""
    regex: bool = False
    days: set[Date] = field(default_factory=set)
    sessions: set[str] = field(default_factory=set)
    tracks: set[str] = field(default_factory=set)
    types: set[str] = field(default_factory=set)
    material: set[str] = field(default_factory=set)


def _matcher(spec: FilterSpec):
    if not spec.text:
        return lambda _: True
    if spec.regex:
        try:
            pattern = re.compile(spec.text, re.I)
        except re.error as exc:
            raise InvalidPattern(str(exc)) from exc
        return lambda haystack: pattern.search(haystack) is not None
    needle = spec.text.lower()
    return lambda haystack: needle in haystack.lower()


def visible_talks(event: Event, spec: FilterSpec) -> list[Talk]:
    """Return the talks the filter shows, in agenda order."""
    matches = _matcher(spec)
    found: list[Talk] = []

    for day in event.days:
        if spec.days and day.date not in spec.days:
            continue
        for session in day.sessions:
            if spec.sessions and session.title not in spec.sessions:
                continue
            for talk in session.talks:
                if spec.tracks and talk.track not in spec.tracks:
                    continue
                if spec.types and talk.contribution_type not in spec.types:
                    continue
                if spec.material and material_state(talk) not in spec.material:
                    continue
                fields = [talk.title, *talk.speakers, session.title]
                if any(matches(f) for f in fields):
                    found.append(talk)
    return found


def facets(event: Event) -> dict[str, list]:
    """The distinct values available to the structured filters."""
    days = [day.date for day in event.days]
    # Deduplicated by title: the session filter selects by title, so one
    # facet entry already covers every session slot that shares it.
    sessions = {s.title for day in event.days for s in day.sessions}
    tracks = {t.track for t in event.all_talks() if t.track}
    types = {t.contribution_type for t in event.all_talks() if t.contribution_type}
    return {
        "days": sorted(days),
        "sessions": sorted(sessions),
        "tracks": sorted(tracks),
        "types": sorted(types),
        "material": ["pdf", "convert", "none"],
        "formats": sorted(FORMAT_ORDER),
    }
