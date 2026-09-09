"""On-disk state: configuration, cached scans, host regimes, and credentials."""
from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, field
from datetime import date as Date
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import platformdirs

from indico_rag.model import Attachment, Day, Event, Session, Talk

APP = "indico-rag"
DEFAULT_OUTPUT_ROOT = Path.home() / "Desktop" / "indico-talks"

_TOML_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}


def _toml_string(value: str) -> str:
    """Render value as a quoted TOML basic string, escaping backslashes and quotes.

    A Windows-style path or a value containing '"' would otherwise produce
    invalid TOML, since backslash introduces an escape sequence in a basic
    string.
    """
    body = "".join(_TOML_ESCAPES.get(ch, ch) for ch in value)
    return f'"{body}"'


def state_root(root: Path | None = None) -> Path:
    path = root or Path(platformdirs.user_state_dir(APP))
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_root(root: Path | None = None) -> Path:
    path = root or Path(platformdirs.user_config_dir(APP))
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass
class Config:
    output_root: Path = DEFAULT_OUTPUT_ROOT
    download_concurrency: int = 8
    convert_concurrency: int = 2
    extract_text: bool = False
    default_host: str = ""


def load_config(root: Path | None = None) -> Config:
    path = config_root(root) / "config.toml"
    if not path.exists():
        return Config()
    data = tomllib.loads(path.read_text())
    return Config(
        output_root=Path(data.get("output_root", str(DEFAULT_OUTPUT_ROOT))).expanduser(),
        download_concurrency=int(data.get("download_concurrency", 8)),
        convert_concurrency=int(data.get("convert_concurrency", 2)),
        extract_text=bool(data.get("extract_text", False)),
        default_host=str(data.get("default_host", "")),
    )


def save_config(config: Config, root: Path | None = None) -> None:
    path = config_root(root) / "config.toml"
    path.write_text(
        f"output_root = {_toml_string(str(config.output_root))}\n"
        f"download_concurrency = {config.download_concurrency}\n"
        f"convert_concurrency = {config.convert_concurrency}\n"
        f"extract_text = {str(config.extract_text).lower()}\n"
        f"default_host = {_toml_string(config.default_host)}\n"
    )


def _host_key(host: str) -> str:
    netloc = urlparse(host).netloc or host
    return netloc.replace(":", "_")


def _dt(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _undt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _attachment_json(a: Attachment) -> dict:
    return {"id": a.id, "filename": a.filename, "kind": a.kind, "url": a.url, "size": a.size,
            "content_type": a.content_type, "uploaded": _dt(a.uploaded),
            "folder_title": a.folder_title}


def _attachment_obj(d: dict) -> Attachment:
    return Attachment(id=d["id"], filename=d["filename"], kind=d["kind"], url=d["url"],
                      size=d.get("size"), content_type=d.get("content_type"),
                      uploaded=_undt(d.get("uploaded")), folder_title=d.get("folder_title", ""))


def event_to_jsonable(event: Event) -> dict:
    return {
        "id": event.id, "host": event.host, "title": event.title, "slug": event.slug,
        "days": [
            {
                "date": day.date.isoformat(),
                "sessions": [
                    {
                        "id": s.id, "title": s.title, "slug": s.slug, "synthetic": s.synthetic,
                        "talks": [
                            {
                                "id": t.id, "contribution_id": t.contribution_id,
                                "title": t.title, "start": _dt(t.start), "end": _dt(t.end),
                                "abstract": t.abstract, "speakers": t.speakers,
                                "speaker_surnames": t.speaker_surnames,
                                "affiliations": t.affiliations, "track": t.track,
                                "contribution_type": t.contribution_type, "url": t.url,
                                "session_title": t.session_title,
                                "session_is_synthetic": t.session_is_synthetic,
                                "attachments": [_attachment_json(a) for a in t.attachments],
                                "chosen": t.chosen.id if t.chosen else None,
                            }
                            for t in s.talks
                        ],
                    }
                    for s in day.sessions
                ],
            }
            for day in event.days
        ],
    }


def event_from_jsonable(data: dict) -> Event:
    days = []
    for raw_day in data.get("days", []):
        sessions = []
        for raw_session in raw_day.get("sessions", []):
            talks = []
            for rt in raw_session.get("talks", []):
                attachments = [_attachment_obj(a) for a in rt.get("attachments", [])]
                chosen_id = rt.get("chosen")
                talks.append(Talk(
                    id=rt["id"], contribution_id=rt["contribution_id"], title=rt["title"],
                    start=_undt(rt.get("start")), end=_undt(rt.get("end")),
                    abstract=rt.get("abstract", ""), speakers=rt.get("speakers", []),
                    speaker_surnames=rt.get("speaker_surnames", []),
                    affiliations=rt.get("affiliations", []), track=rt.get("track", ""),
                    contribution_type=rt.get("contribution_type", ""), url=rt.get("url", ""),
                    session_title=rt.get("session_title", ""),
                    session_is_synthetic=rt.get("session_is_synthetic", False),
                    attachments=attachments,
                    # A rescan may drop the attachment `chosen` used to point
                    # at; an id absent from `attachments` rehydrates as None
                    # rather than raising.
                    chosen=next((a for a in attachments if a.id == chosen_id), None),
                ))
            sessions.append(Session(id=raw_session["id"], title=raw_session["title"],
                                    slug=raw_session["slug"], talks=talks,
                                    synthetic=raw_session.get("synthetic", False)))
        days.append(Day(date=Date.fromisoformat(raw_day["date"]), sessions=sessions))
    return Event(id=data["id"], host=data["host"], title=data["title"], slug=data["slug"],
                 days=days)


@dataclass
class ScanRecord:
    host: str
    event_id: str
    title: str
    first_day: str
    last_day: str
    talk_count: int
    fetched_at: str
    selection: list[str] = field(default_factory=list)
    output_dir: str | None = None


def _scans_dir(root: Path | None) -> Path:
    path = state_root(root) / "scans"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _scan_path(host: str, event_id: str, root: Path | None) -> Path:
    return _scans_dir(root) / f"{_host_key(host)}_{event_id}.json"


def save_scan(event: Event, *, selection: list[str] | None = None,
              output_dir: str | None = None, root: Path | None = None) -> None:
    talks = event.all_talks()
    record = ScanRecord(
        host=event.host, event_id=event.id, title=event.title,
        first_day=event.days[0].date.isoformat() if event.days else "",
        last_day=event.days[-1].date.isoformat() if event.days else "",
        talk_count=len(talks), fetched_at=datetime.now().isoformat(timespec="seconds"),
        selection=selection or [], output_dir=output_dir,
    )
    payload = {"record": record.__dict__, "event": event_to_jsonable(event)}
    _scan_path(event.host, event.id, root).write_text(json.dumps(payload, indent=2))

    # Read the current index before writing so a save of one event never
    # drops the entries of others.
    index = {f"{_host_key(r.host)}_{r.event_id}": r.__dict__ for r in list_scans(root)}
    index[f"{_host_key(event.host)}_{event.id}"] = record.__dict__
    (_scans_dir(root) / "index.json").write_text(json.dumps(index, indent=2))


def load_scan(host: str, event_id: str, root: Path | None = None) -> tuple[Event, ScanRecord]:
    payload = json.loads(_scan_path(host, event_id, root).read_text())
    return event_from_jsonable(payload["event"]), ScanRecord(**payload["record"])


def list_scans(root: Path | None = None) -> list[ScanRecord]:
    path = _scans_dir(root) / "index.json"
    if not path.exists():
        return []
    entries = json.loads(path.read_text()).values()
    records = [ScanRecord(**entry) for entry in entries]
    return sorted(records, key=lambda r: r.fetched_at, reverse=True)


def delete_scan(host: str, event_id: str, root: Path | None = None) -> None:
    _scan_path(host, event_id, root).unlink(missing_ok=True)
    path = _scans_dir(root) / "index.json"
    if path.exists():
        index = json.loads(path.read_text())
        index.pop(f"{_host_key(host)}_{event_id}", None)
        path.write_text(json.dumps(index, indent=2))


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def _write_private(path: Path, text: str) -> None:
    """Write text to a file no one but its owner can read.

    The mode is supplied to open(2) so the file is never created readable and
    then narrowed; chmod additionally covers a file that already exists.
    """
    descriptor = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(text)
    os.chmod(path, 0o600)


def is_known_host(host: str, root: Path | None = None) -> bool:
    """Report whether this host has been used before.

    A host counts as known once it holds a token or has produced a scan, which
    is what distinguishes a first visit worth asking about from a return one.
    """
    key = _host_key(host)
    if key in _read_json(state_root(root) / "credentials.json"):
        return True
    if key in _read_json(state_root(root) / "hosts.json"):
        return True
    return any(_host_key(record.host) == key for record in list_scans(root))


def get_token(host: str, root: Path | None = None) -> str | None:
    return _read_json(state_root(root) / "credentials.json").get(_host_key(host))


def set_token(host: str, token: str, root: Path | None = None) -> None:
    path = state_root(root) / "credentials.json"
    data = _read_json(path)
    data[_host_key(host)] = token
    _write_private(path, json.dumps(data, indent=2))


def get_host_regime(host: str, root: Path | None = None) -> str | None:
    return _read_json(state_root(root) / "hosts.json").get(_host_key(host))


def set_host_regime(host: str, regime: str, root: Path | None = None) -> None:
    path = state_root(root) / "hosts.json"
    data = _read_json(path)
    data[_host_key(host)] = regime
    path.write_text(json.dumps(data, indent=2))
