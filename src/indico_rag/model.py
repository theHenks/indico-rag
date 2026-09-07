"""The agenda model, kept independent of Indico's wire format."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as Date
from datetime import datetime


@dataclass
class Attachment:
    id: str
    filename: str
    kind: str  # "file" or "link"
    url: str
    size: int | None = None
    content_type: str | None = None
    uploaded: datetime | None = None
    folder_title: str = ""

    @property
    def suffix(self) -> str:
        _, _, ext = self.filename.rpartition(".")
        return ext.lower() if ext and ext != self.filename else ""


@dataclass
class Talk:
    id: str
    contribution_id: str
    title: str
    start: datetime | None
    end: datetime | None
    abstract: str = ""
    speakers: list[str] = field(default_factory=list)
    speaker_surnames: list[str] = field(default_factory=list)
    affiliations: list[str] = field(default_factory=list)
    track: str = ""
    contribution_type: str = ""
    url: str = ""
    session_title: str = ""
    session_is_synthetic: bool = False
    attachments: list[Attachment] = field(default_factory=list)
    chosen: Attachment | None = None


@dataclass
class Session:
    id: str
    title: str
    slug: str
    talks: list[Talk] = field(default_factory=list)
    synthetic: bool = False


@dataclass
class Day:
    date: Date
    sessions: list[Session] = field(default_factory=list)


@dataclass
class Event:
    id: str
    host: str
    title: str
    slug: str
    days: list[Day] = field(default_factory=list)

    def all_talks(self) -> list[Talk]:
        return [talk for day in self.days for session in day.sessions for talk in session.talks]
