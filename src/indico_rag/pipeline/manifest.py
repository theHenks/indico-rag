"""The record of what was downloaded, converted and skipped.

Doubles as the resume record and as the metadata a retrieval pipeline indexes.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

MANIFEST_NAME = "_manifest.json"

_KNOWN_STATUSES = ("done", "failed", "no_material")


def sha256_of(path: Path) -> str:
    """Hash `path` in fixed-size chunks so multi-hundred-megabyte decks never
    have to be read into memory whole."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class TalkRecord:
    contribution_id: str
    title: str
    abstract: str
    speakers: list[str]
    affiliations: list[str]
    session: str
    track: str
    contribution_type: str
    start: str | None
    end: str | None
    indico_url: str
    source_url: str
    source_filename: str
    local_path: str | None
    sha256: str | None
    bytes: int | None
    converted: bool
    conversion_error: str | None
    # kw_only so a default is possible here without forcing one on every field
    # declared after it; every TalkRecord() call site already uses keywords.
    extraction_error: str | None = field(default=None, kw_only=True)
    text_path: str | None
    text_chars: int | None
    status: str
    error: str | None


@dataclass(frozen=True)
class EventInfo:
    """The event a manifest belongs to. `host` includes the scheme."""
    host: str
    id: str
    title: str

    @property
    def url(self) -> str:
        return f"{self.host}/event/{self.id}/"


@dataclass
class Manifest:
    path: Path
    records: dict[str, TalkRecord] = field(default_factory=dict)
    event: EventInfo | None = None

    @classmethod
    def load(cls, path: Path) -> "Manifest":
        if not path.exists():
            return cls(path=path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        records = {key: TalkRecord(**value) for key, value in payload.get("talks", {}).items()}
        raw_event = payload.get("event")
        event = None
        if raw_event is not None:
            event = EventInfo(host=raw_event["host"], id=raw_event["id"], title=raw_event["title"])
        return cls(path=path, records=records, event=event)

    def add(self, record: TalkRecord) -> None:
        self.records[record.contribution_id] = record

    def get(self, contribution_id: str) -> TalkRecord | None:
        return self.records.get(contribution_id)

    def is_complete(self, contribution_id: str) -> bool:
        record = self.records.get(contribution_id)
        if record is None or record.status != "done" or not record.local_path:
            return False
        path = Path(record.local_path)
        if not path.exists():
            return False
        return record.sha256 == sha256_of(path)

    def counts(self) -> dict[str, int]:
        tally = Counter(record.status for record in self.records.values())
        for status in _KNOWN_STATUSES:
            tally.setdefault(status, 0)
        return dict(tally)

    def save(self) -> None:
        """Write the manifest atomically: a crash mid-write must never leave
        a truncated file that fails to parse on the next resume, which would
        discard every record written so far."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict = {}
        if self.event is not None:
            payload["event"] = {**asdict(self.event), "url": self.event.url}
        payload["counts"] = self.counts()
        payload["talks"] = {key: asdict(value) for key, value in self.records.items()}
        fd, tmp_name = tempfile.mkstemp(
            dir=self.path.parent, prefix=f".{self.path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.path)
        except BaseException:
            os.unlink(tmp_name)
            raise
