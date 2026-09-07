from datetime import date, datetime

from indico_rag.model import Day, Event, Session, Talk
from indico_rag.tui.selection import SelectionState


def _event() -> Event:
    def talk(i, session_title):
        return Talk(id=f"c{i}", contribution_id=str(i), title=f"Talk {i}",
                    start=datetime(2026, 8, 31, 9 + i, 0), end=None,
                    session_title=session_title)

    s1 = Session(id="s1", title="Plenary", slug="p", talks=[talk(1, "Plenary"), talk(2, "Plenary")])
    s2 = Session(id="s2", title="Parallel", slug="q", talks=[talk(3, "Parallel")])
    return Event(id="1", host="h", title="E", slug="e",
                 days=[Day(date=date(2026, 8, 31), sessions=[s1, s2])])


def test_a_new_selection_is_empty():
    state = SelectionState(_event())
    assert state.count == 0
    assert state.day_state(_event().days[0]) == "none"


def test_toggling_a_talk_flips_it():
    event = _event()
    state = SelectionState(event)
    state.toggle_talk("1")
    assert state.is_selected("1") is True
    state.toggle_talk("1")
    assert state.is_selected("1") is False


def test_a_session_is_partial_when_only_some_talks_are_selected():
    event = _event()
    state = SelectionState(event)
    state.toggle_talk("1")
    plenary = event.days[0].sessions[0]
    assert state.session_state(plenary) == "partial"
    state.toggle_talk("2")
    assert state.session_state(plenary) == "all"


def test_toggling_a_session_selects_all_its_talks_then_clears_them():
    event = _event()
    state = SelectionState(event)
    plenary = event.days[0].sessions[0]
    state.toggle_session(plenary)
    assert state.selected == {"1", "2"}
    state.toggle_session(plenary)
    assert state.selected == set()


def test_toggling_a_day_covers_every_session():
    event = _event()
    state = SelectionState(event)
    state.toggle_day(event.days[0])
    assert state.selected == {"1", "2", "3"}
    assert state.day_state(event.days[0]) == "all"


def test_a_partially_selected_day_toggles_to_fully_selected():
    event = _event()
    state = SelectionState(event)
    state.toggle_talk("1")
    assert state.day_state(event.days[0]) == "partial"
    state.toggle_day(event.days[0])
    assert state.selected == {"1", "2", "3"}


def test_an_empty_session_reports_no_state_and_toggle_is_a_no_op():
    event = _event()
    state = SelectionState(event)
    empty = Session(id="s3", title="Empty", slug="empty", talks=[])
    assert state.session_state(empty) == "none"
    state.toggle_session(empty)
    assert state.selected == set()
