import json
from pathlib import Path

import httpx
import respx

from indico_rag.scan import format_agenda, scan_event

FIXTURES = Path(__file__).parent / "fixtures"
HOST = "https://agenda.infn.it"


@respx.mock
async def test_scan_returns_a_ranked_event_and_reports_progress():
    timetable = json.loads((FIXTURES / "timetable_50058.json").read_text())
    export = json.loads((FIXTURES / "event_50058_contributions.json").read_text())
    respx.get(f"{HOST}/export/timetable/50058.json").mock(
        return_value=httpx.Response(200, json=timetable)
    )
    respx.get(f"{HOST}/export/event/50058.json").mock(
        return_value=httpx.Response(200, json=export)
    )

    stages: list[str] = []
    event = await scan_event(HOST, "50058", progress=stages.append)

    assert event.days
    assert event.all_talks()
    assert any("timetable" in s for s in stages)
    # apply_ranking ran: any talk with attachments has a chosen file.
    with_attachments = [t for t in event.all_talks() if t.attachments]
    assert all(t.chosen is not None for t in with_attachments if
               any(a.suffix in {"pdf", "pptx", "ppt", "odp", "key"} for a in t.attachments))


@respx.mock
async def test_scan_event_surfaces_a_scope_warning_from_the_client():
    timetable = json.loads((FIXTURES / "timetable_50058.json").read_text())
    export = json.loads((FIXTURES / "event_50058_contributions.json").read_text())

    def timetable_responder(request: httpx.Request) -> httpx.Response:
        if "authorization" in request.headers:
            return httpx.Response(400, json={"error": "insufficient_scope"})
        return httpx.Response(200, json=timetable)

    respx.get(f"{HOST}/export/timetable/50058.json").mock(side_effect=timetable_responder)
    respx.get(f"{HOST}/export/event/50058.json").mock(
        return_value=httpx.Response(200, json=export)
    )

    stages: list[str] = []
    event = await scan_event(HOST, "50058", token="indp_badscope", progress=stages.append)

    assert event.days
    assert any("Classic API" in s for s in stages)


def test_format_agenda_shows_days_sessions_and_chosen_files():
    from datetime import date, datetime

    from indico_rag.model import Attachment, Day, Event, Session, Talk

    attachment = Attachment(id="a", filename="deck.pdf", kind="file", url="https://x/deck.pdf")
    talk = Talk(id="c1", contribution_id="11", title="Welcome",
                start=datetime(2026, 8, 31, 9, 0), end=None, speakers=["Maria Rossi"],
                attachments=[attachment], chosen=attachment)
    empty = Talk(id="c2", contribution_id="12", title="Discussion", start=None, end=None)
    event = Event(id="50058", host=HOST, title="Workshop", slug="workshop",
                  days=[Day(date=date(2026, 8, 31),
                            sessions=[Session(id="s1", title="Plenary", slug="plenary",
                                              talks=[talk, empty])])])

    text = format_agenda(event)
    assert "2026-08-31" in text
    assert "Plenary" in text
    assert "09:00" in text
    assert "deck.pdf" in text
    assert "no material" in text


def test_format_agenda_matches_the_captured_events_measured_counts():
    from indico_rag.indico.parser import attachments_by_contribution, parse_timetable
    from indico_rag.indico.ranking import apply_ranking

    timetable = json.loads((FIXTURES / "timetable_50058.json").read_text())
    export = json.loads((FIXTURES / "event_50058_contributions.json").read_text())
    event = parse_timetable(
        timetable,
        host=HOST,
        event_id="50058",
        event_title="NOW 2026",
        attachments=attachments_by_contribution(export),
    )
    apply_ranking(event)

    text = format_agenda(event)
    lines = text.splitlines()

    date_headings = [line for line in lines if len(line) == 10 and line[4] == "-" and line[7] == "-"]
    # The timetable carries 7 day keys, but two ("Arrival and Registration" on
    # 08-31 and "Departures" on 09-06) hold a session with no contributions;
    # parse_timetable drops a session with no talks, and a day left with no
    # sessions, so 5 days reach the printed agenda.
    assert len(date_headings) == 5

    no_material_lines = [line for line in lines if "no material" in line]
    assert len(no_material_lines) == 1

    conversion_lines = [line for line in lines if "→ pdf" in line]
    assert len(conversion_lines) == 2
