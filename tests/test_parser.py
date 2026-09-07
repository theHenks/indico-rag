import json
from pathlib import Path

from indico_rag.indico.parser import attachments_by_contribution, parse_timetable

FIXTURES = Path(__file__).parent / "fixtures"

SYNTHETIC = {
    "results": {
        "50058": {
            "20260831": {
                "s1": {
                    "entryType": "Session",
                    "id": "s1",
                    "title": "Plenary Session I",
                    "sessionSlug": "plenary-i",
                    "entries": {
                        "c1": {
                            "entryType": "Contribution",
                            "id": "c1",
                            "contributionId": "11",
                            "title": "Welcome and Overview",
                            "description": "Opening remarks.",
                            "startDate": {"date": "2026-08-31", "time": "09:00:00"},
                            "endDate": {"date": "2026-08-31", "time": "09:30:00"},
                            "presenters": [
                                {"firstName": "Maria", "familyName": "Colomer Rossi",
                                 "affiliation": "INFN", "name": "Colomer Rossi, Maria"}
                            ],
                            "url": "/event/50058/contributions/11/",
                            "attachments": {
                                "files": [
                                    {"id": "800", "title": "inline.pdf",
                                     "download_url": "/event/50058/x/inline.pdf"}
                                ],
                                "folders": [],
                            },
                        },
                        "b1": {
                            "entryType": "Break",
                            "id": "b1",
                            "title": "Coffee",
                            "startDate": {"date": "2026-08-31", "time": "10:30:00"},
                        },
                    },
                },
                "c2": {
                    "entryType": "Contribution",
                    "id": "c2",
                    "contributionId": "12",
                    "title": "Unscheduled Talk",
                    "startDate": {"date": "2026-08-31", "time": "11:00:00"},
                    "endDate": {"date": "2026-08-31", "time": "11:20:00"},
                    "presenters": [],
                },
            }
        }
    }
}

EXPORT = {
    "results": [
        {
            "contributions": [
                {
                    "id": "9",
                    "db_id": 11,
                    "folders": [
                        {
                            "title": "Slides",
                            "attachments": [
                                {
                                    "id": "901",
                                    "title": "Welcome",
                                    "filename": "welcome.pdf",
                                    "download_url": "https://agenda.infn.it/x/welcome.pdf",
                                    "size": 12345,
                                    "content_type": "application/pdf",
                                    "type": "file",
                                    "modified_dt": "2026-08-20T10:00:00+00:00",
                                }
                            ],
                        }
                    ],
                }
            ]
        }
    ]
}


def test_breaks_are_dropped_and_sessions_are_preserved():
    event = parse_timetable(
        SYNTHETIC, host="https://agenda.infn.it", event_id="50058", event_title="Workshop"
    )
    assert len(event.days) == 1
    day = event.days[0]
    assert str(day.date) == "2026-08-31"
    titles = {s.title for s in day.sessions}
    assert "Plenary Session I" in titles
    all_titles = [t.title for t in event.all_talks()]
    assert "Coffee" not in all_titles
    assert "Welcome and Overview" in all_titles


def test_a_contribution_outside_any_session_gets_a_synthetic_session():
    event = parse_timetable(
        SYNTHETIC, host="https://agenda.infn.it", event_id="50058", event_title="Workshop"
    )
    holder = [s for s in event.days[0].sessions if any(t.title == "Unscheduled Talk" for t in s.talks)]
    assert len(holder) == 1
    assert holder[0].talks[0].session_is_synthetic is True


def test_speakers_and_times_are_parsed():
    event = parse_timetable(
        SYNTHETIC, host="https://agenda.infn.it", event_id="50058", event_title="Workshop"
    )
    talk = next(t for t in event.all_talks() if t.title == "Welcome and Overview")
    assert talk.speakers == ["Maria Colomer Rossi"]
    # Multi-word surnames must survive; splitting a full name on whitespace loses them.
    assert talk.speaker_surnames == ["Colomer Rossi"]
    assert talk.affiliations == ["INFN"]
    assert talk.start.strftime("%H%M") == "0900"


def test_attachments_are_joined_on_db_id_not_the_friendly_id():
    # The export's own `id` is a friendly number matching nothing in the timetable.
    attachments = attachments_by_contribution(EXPORT)
    assert set(attachments) == {"11"}
    event = parse_timetable(
        SYNTHETIC,
        host="https://agenda.infn.it",
        event_id="50058",
        event_title="Workshop",
        attachments=attachments,
    )
    talk = next(t for t in event.all_talks() if t.contribution_id == "11")
    assert len(talk.attachments) == 1
    # The export wins over the inline copy: it carries size and content type.
    assert talk.attachments[0].filename == "welcome.pdf"
    assert talk.attachments[0].folder_title == "Slides"
    assert talk.attachments[0].size == 12345


def test_inline_timetable_attachments_are_used_when_the_export_lacks_the_talk():
    event = parse_timetable(
        SYNTHETIC, host="https://agenda.infn.it", event_id="50058", event_title="Workshop",
        attachments={},
    )
    talk = next(t for t in event.all_talks() if t.contribution_id == "11")
    assert [a.filename for a in talk.attachments] == ["inline.pdf"]


def test_a_null_folder_title_becomes_an_empty_string():
    export = {"results": [{"contributions": [
        {"db_id": 11, "folders": [
            {"title": None, "attachments": [
                {"id": "1", "filename": "a.pdf", "download_url": "https://x/a.pdf",
                 "type": "file"}
            ]}
        ]}
    ]}]}
    assert attachments_by_contribution(export)["11"][0].folder_title == ""


def test_the_captured_event_parses_into_a_non_empty_agenda():
    timetable = json.loads((FIXTURES / "timetable_50058.json").read_text())
    export = json.loads((FIXTURES / "event_50058_contributions.json").read_text())
    event = parse_timetable(
        timetable,
        host="https://agenda.infn.it",
        event_id="50058",
        event_title="captured",
        attachments=attachments_by_contribution(export),
    )
    assert event.days, "the captured timetable produced no days"
    talks = event.all_talks()
    assert talks, "the captured timetable produced no talks"
    assert all(t.title for t in talks), "every talk must have a title"
    assert any(t.attachments for t in talks), "no talk in the capture has attachments"
    # The join must actually land on the export, whose attachments carry size.
    # Joining on the wrong identifier yields inline-only attachments with no size.
    assert any(a.size for t in talks for a in t.attachments), \
        "attachments carry no size: the export join failed"
    assert len(talks) == 100, f"expected 100 talks in the capture, got {len(talks)}"
