import json
from collections import Counter
from datetime import date, datetime
from pathlib import Path

import pytest

from indico_rag.indico.parser import attachments_by_contribution, parse_timetable
from indico_rag.indico.ranking import apply_ranking
from indico_rag.model import Attachment, Day, Event, Session, Talk
from indico_rag.tui.filtering import (
    FilterSpec,
    InvalidPattern,
    facets,
    material_state,
    visible_talks,
)
from indico_rag.tui.selection import SelectionState

FIXTURES = Path(__file__).parent / "fixtures"


def _event() -> Event:
    pdf = Attachment(id="a1", filename="deck.pdf", kind="file", url="u")
    pptx = Attachment(id="a2", filename="deck.pptx", kind="file", url="u")

    neutrino = Talk(id="c1", contribution_id="1", title="Neutrino Mass Limits",
                    start=datetime(2026, 8, 31, 9, 0), end=None, speakers=["Li Chen"],
                    track="Neutrinos", contribution_type="Oral", session_title="Plenary",
                    attachments=[pdf], chosen=pdf)
    background = Talk(id="c2", contribution_id="2", title="Background Modeling",
                      start=datetime(2026, 8, 31, 11, 0), end=None, speakers=["A. Müller"],
                      track="Backgrounds", contribution_type="Oral", session_title="Plenary",
                      attachments=[pptx], chosen=pptx)
    panel = Talk(id="c3", contribution_id="3", title="Discussion",
                 start=datetime(2026, 9, 1, 14, 0), end=None, track="",
                 contribution_type="Panel", session_title="Closing")

    day1 = Day(date=date(2026, 8, 31),
               sessions=[Session(id="s1", title="Plenary", slug="p",
                                 talks=[neutrino, background])])
    day2 = Day(date=date(2026, 9, 1),
               sessions=[Session(id="s2", title="Closing", slug="c", talks=[panel])])
    return Event(id="1", host="h", title="E", slug="e", days=[day1, day2])


def test_material_state_classifies_each_talk():
    event = _event()
    talks = {t.title: t for t in event.all_talks()}
    assert material_state(talks["Neutrino Mass Limits"]) == "pdf"
    assert material_state(talks["Background Modeling"]) == "convert"
    assert material_state(talks["Discussion"]) == "none"


def test_an_empty_spec_shows_everything():
    event = _event()
    assert len(visible_talks(event, FilterSpec())) == 3


def test_substring_matching_is_case_insensitive():
    event = _event()
    found = visible_talks(event, FilterSpec(text="neutrino"))
    assert [t.title for t in found] == ["Neutrino Mass Limits"]


def test_substring_matching_covers_speaker_and_session():
    event = _event()
    assert len(visible_talks(event, FilterSpec(text="chen"))) == 1
    assert len(visible_talks(event, FilterSpec(text="plenary"))) == 2


def test_regex_mode_matches_a_pattern():
    event = _event()
    found = visible_talks(event, FilterSpec(text=r"^Back.*Modeling$", regex=True))
    assert [t.title for t in found] == ["Background Modeling"]


def test_a_regex_pattern_is_a_literal_string_in_substring_mode():
    event = _event()
    assert visible_talks(event, FilterSpec(text=r"^Back.*Modeling$")) == []


def test_an_invalid_regex_raises_rather_than_falling_back_to_substring():
    event = _event()
    with pytest.raises(InvalidPattern):
        visible_talks(event, FilterSpec(text="Back(", regex=True))


def test_filtering_by_day_track_type_and_material():
    event = _event()
    assert len(visible_talks(event, FilterSpec(days={date(2026, 9, 1)}))) == 1
    assert len(visible_talks(event, FilterSpec(tracks={"Neutrinos"}))) == 1
    assert len(visible_talks(event, FilterSpec(types={"Panel"}))) == 1
    assert len(visible_talks(event, FilterSpec(material={"convert"}))) == 1


def test_structured_filters_combine_conjunctively():
    event = _event()
    spec = FilterSpec(days={date(2026, 8, 31)}, material={"pdf"})
    assert [t.title for t in visible_talks(event, spec)] == ["Neutrino Mass Limits"]


def test_facets_list_the_distinct_values():
    event = _event()
    found = facets(event)
    assert found["days"] == [date(2026, 8, 31), date(2026, 9, 1)]
    assert found["sessions"] == ["Closing", "Plenary"]
    assert found["tracks"] == ["Backgrounds", "Neutrinos"]
    assert found["types"] == ["Oral", "Panel"]


def test_filtering_does_not_change_the_selection():
    event = _event()
    state = SelectionState(event)
    state.toggle_talk("3")
    visible_talks(event, FilterSpec(text="neutrino"))
    assert state.selected == {"3"}


def test_selecting_all_visible_accumulates_across_successive_filters():
    # The workflow this protects: filter to a track, select all, clear, repeat.
    event = _event()
    state = SelectionState(event)

    shown = visible_talks(event, FilterSpec(tracks={"Neutrinos"}))
    state.set_talks([t.contribution_id for t in shown], True)
    assert state.selected == {"1"}

    shown = visible_talks(event, FilterSpec(types={"Panel"}))
    state.set_talks([t.contribution_id for t in shown], True)
    assert state.selected == {"1", "3"}

    assert len(visible_talks(event, FilterSpec())) == 3
    assert state.selected == {"1", "3"}


def test_an_empty_filter_result_does_not_clear_the_selection():
    event = _event()
    state = SelectionState(event)
    state.toggle_talk("1")
    state.toggle_talk("2")
    assert visible_talks(event, FilterSpec(text="no such talk exists")) == []
    assert state.selected == {"1", "2"}


def test_regex_mode_pattern_that_is_also_valid_substring_text():
    event = _event()
    # "Neutrino" matches in both modes; "^Neutrino" only matches as a regex.
    assert len(visible_talks(event, FilterSpec(text="Neutrino"))) == 1
    assert len(visible_talks(event, FilterSpec(text="Neutrino", regex=True))) == 1
    assert visible_talks(event, FilterSpec(text="^Neutrino")) == []
    assert len(visible_talks(event, FilterSpec(text="^Neutrino", regex=True))) == 1


def test_regex_mode_is_case_insensitive():
    event = _event()
    found = visible_talks(event, FilterSpec(text="neutrino", regex=True))
    assert [t.title for t in found] == ["Neutrino Mass Limits"]


def test_the_captured_event_matches_measured_ground_truth():
    timetable = json.loads((FIXTURES / "timetable_50058.json").read_text())
    export = json.loads((FIXTURES / "event_50058_contributions.json").read_text())
    event = parse_timetable(
        timetable,
        host="https://agenda.infn.it",
        event_id="50058",
        event_title="captured",
        attachments=attachments_by_contribution(export),
    )
    apply_ranking(event)

    talks = event.all_talks()
    assert len(talks) == 100
    assert len(event.days) == 5
    # A model property: the agenda has 14 physical Session objects. Four
    # parallel sessions are each split across two days and so appear twice.
    assert sum(len(day.sessions) for day in event.days) == 14

    counts = Counter(material_state(t) for t in talks)
    assert counts == Counter({"pdf": 97, "convert": 2, "none": 1})

    found = facets(event)
    assert len(found["days"]) == 5
    # A menu property: the session filter matches by title, so the two
    # day-slots of each split parallel session collapse to one facet entry.
    assert len(found["sessions"]) == 10
    assert found["tracks"] == []
    assert found["types"] == []

    # One facet entry must still cover every session slot sharing its title:
    # this title spans two day-slots (9 talks each), so both are returned.
    split_title = "Session I (parallel) - Standard three-neutrino oscillations"
    assert split_title in found["sessions"]
    spec = FilterSpec(sessions={split_title})
    assert len(visible_talks(event, spec)) == 18
