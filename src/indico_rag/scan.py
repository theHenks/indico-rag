"""Fetch an event's timetable and turn it into a ranked agenda."""
from __future__ import annotations

from collections.abc import Callable

from indico_rag.client import IndicoClient
from indico_rag.indico.endpoints import contributions_path, timetable_path
from indico_rag.indico.parser import attachments_by_contribution, parse_timetable
from indico_rag.indico.ranking import apply_ranking
from indico_rag.model import Event

Progress = Callable[[str], None]


async def scan_event(host: str, event_id: str, *, token: str | None = None,
                     progress: Progress | None = None) -> Event:
    def report(message: str) -> None:
        if progress is not None:
            progress(message)

    async with IndicoClient(host, token=token) as client:
        report("fetching timetable")
        timetable = await client.get_json(timetable_path(event_id))
        if client.warning is not None:
            report(client.warning)
        report(f"access regime: {client.regime}")

        report("fetching attachments")
        export = await client.get_json(
            contributions_path(event_id), params={"detail": "contributions", "occ": "yes"}
        )

    attachments = attachments_by_contribution(export)
    title = _event_title(export, event_id)
    event = parse_timetable(timetable, host=host, event_id=event_id, event_title=title,
                            attachments=attachments)
    apply_ranking(event)
    report(f"parsed {len(event.all_talks())} talks across {len(event.days)} days")
    return event


def _event_title(export: dict, event_id: str) -> str:
    for result in export.get("results", []):
        title = result.get("title")
        if title:
            return str(title)
    return f"event-{event_id}"


def format_agenda(event: Event) -> str:
    lines = [f"{event.title}  [{event.host}/event/{event.id}]", ""]
    for day in event.days:
        lines.append(f"{day.date.isoformat()}")
        for session in day.sessions:
            lines.append(f"  {session.title}")
            for talk in session.talks:
                clock = talk.start.strftime("%H:%M") if talk.start else "  :  "
                who = f" — {talk.speakers[0]}" if talk.speakers else ""
                if talk.chosen is None:
                    marker = "no material"
                elif talk.chosen.kind == "link":
                    marker = "google slides → pdf"
                elif talk.chosen.suffix == "pdf":
                    marker = talk.chosen.filename
                else:
                    marker = f"{talk.chosen.filename} → pdf"
                lines.append(f"    {clock}  {talk.title}{who}  [{marker}]")
        lines.append("")
    return "\n".join(lines)
