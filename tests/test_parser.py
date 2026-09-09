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

# The outer day dict lists the afternoon session ("s2") before the morning one
# ("s1"), and s1's own entries list its 10:00 talk before its 09:00 talk.
# Indico's JSON gives no ordering guarantee on either dict, so a correct parse
# must not rely on dict-insertion order for either sessions or talks.
UNORDERED = {
    "results": {
        "50058": {
            "20260901": {
                "s2": {
                    "entryType": "Session",
                    "id": "s2",
                    "title": "Afternoon Session",
                    "entries": {
                        "c3": {
                            "entryType": "Contribution",
                            "id": "c3",
                            "contributionId": "23",
                            "title": "Afternoon Talk",
                            "startDate": {"date": "2026-09-01", "time": "09:30:00"},
                            "endDate": {"date": "2026-09-01", "time": "09:50:00"},
                            "presenters": [],
                        },
                    },
                },
                "s1": {
                    "entryType": "Session",
                    "id": "s1",
                    "title": "Morning Session",
                    "entries": {
                        "c2": {
                            "entryType": "Contribution",
                            "id": "c2",
                            "contributionId": "12",
                            "title": "Morning Talk Two",
                            "startDate": {"date": "2026-09-01", "time": "10:00:00"},
                            "endDate": {"date": "2026-09-01", "time": "10:20:00"},
                            "presenters": [],
                        },
                        "c1": {
                            "entryType": "Contribution",
                            "id": "c1",
                            "contributionId": "11",
                            "title": "Morning Talk One",
                            "startDate": {"date": "2026-09-01", "time": "09:00:00"},
                            "endDate": {"date": "2026-09-01", "time": "09:20:00"},
                            "presenters": [],
                        },
                    },
                },
            }
        }
    }
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


def test_sessions_and_talks_are_ordered_by_start_time_not_dict_order():
    event = parse_timetable(
        UNORDERED, host="https://agenda.infn.it", event_id="50058", event_title="Workshop"
    )
    day = event.days[0]
    assert [s.title for s in day.sessions] == ["Morning Session", "Afternoon Session"]
    morning = day.sessions[0]
    assert [t.title for t in morning.talks] == ["Morning Talk One", "Morning Talk Two"]


def test_attachments_by_contribution_tolerates_explicit_nulls():
    # Indico emits explicit `null` in place of an empty list or string in
    # several export payloads (see contribution 304612 in the captured
    # fixture, whose "files" is null rather than []).
    export = {
        "results": [
            {
                "contributions": [
                    {"db_id": 11, "folders": None},
                    {"db_id": 12, "folders": [
                        {"title": None, "attachments": None},
                    ]},
                    {"db_id": 13, "folders": [
                        {"title": None, "attachments": [
                            {"id": "1", "filename": "a.pdf",
                             "download_url": "https://x/a.pdf", "type": "file"}
                        ]},
                    ]},
                ]
            }
        ]
    }
    attachments = attachments_by_contribution(export)
    assert "11" not in attachments
    assert "12" not in attachments
    assert attachments["13"][0].folder_title == ""


def test_two_slots_of_one_session_in_a_day_keep_all_their_talks():
    """A session split into two blocks in one day must not lose a block.

    Indico gives each slot its own timetable entry while both carry the same
    session id, so keying sessions by that id and assigning discards whichever
    slot is parsed first, silently halving the day.
    """
    timetable = {
        "results": {
            "9": {
                "20260301": {
                    "s-morning": {
                        "entryType": "Session",
                        "id": "s-morning",
                        "sessionId": 42,
                        "title": "Hardware",
                        "entries": {
                            "c1": {
                                "entryType": "Contribution", "id": "c1",
                                "contributionId": "1", "title": "Morning talk",
                                "startDate": {"date": "2026-03-01", "time": "09:00:00"},
                                "endDate": {"date": "2026-03-01", "time": "09:30:00"},
                            }
                        },
                    },
                    "s-afternoon": {
                        "entryType": "Session",
                        "id": "s-afternoon",
                        "sessionId": 42,
                        "title": "Hardware",
                        "entries": {
                            "c2": {
                                "entryType": "Contribution", "id": "c2",
                                "contributionId": "2", "title": "Afternoon talk",
                                "startDate": {"date": "2026-03-01", "time": "14:00:00"},
                                "endDate": {"date": "2026-03-01", "time": "14:30:00"},
                            }
                        },
                    },
                }
            }
        }
    }

    event = parse_timetable(timetable, host="https://x", event_id="9", event_title="E")
    titles = [talk.title for talk in event.all_talks()]
    assert titles == ["Morning talk", "Afternoon talk"], titles
    # Both slots belong to one session, so they share a folder rather than
    # producing two identically named ones.
    assert len(event.days[0].sessions) == 1


def test_a_link_attachment_keeps_its_target_not_the_redirect():
    """A link attachment must carry where the material really is.

    Indico exposes both the destination and a redirect of its own standing in
    front of it. Keeping the redirect hides what the link points at, so a
    Google Slides deck cannot be recognised or exported and the talk reads as
    having no material.
    """
    export = {"results": [{"contributions": [
        {"db_id": 11, "folders": [{"title": "Slides", "attachments": [
            {"id": "1", "title": "Deck", "type": "link",
             "download_url": "https://indico.example.org/event/1/attachments/5/8/go",
             "link_url": "https://docs.google.com/presentation/d/ABC123/edit"}
        ]}]}
    ]}]}

    attachment = attachments_by_contribution(export)["11"][0]
    assert attachment.kind == "link"
    assert attachment.url == "https://docs.google.com/presentation/d/ABC123/edit"


def test_a_google_slides_link_is_chosen_and_exported_as_pdf():
    from indico_rag.indico.ranking import choose, google_slides_pdf_url
    from indico_rag.model import Attachment, Talk

    link = Attachment(id="l", filename="Deck", kind="link",
                      url="https://docs.google.com/presentation/d/ABC123/edit")
    talk = Talk(id="c1", contribution_id="1", title="T", start=None, end=None,
                attachments=[link])
    chosen = choose(talk)
    assert chosen is link, "a Google Slides link is usable material"
    assert google_slides_pdf_url(chosen.url) == (
        "https://docs.google.com/presentation/d/ABC123/export/pdf"
    )
