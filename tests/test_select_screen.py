import json
from collections import Counter
from datetime import date, datetime
from pathlib import Path

from indico_rag.indico.parser import attachments_by_contribution, parse_timetable
from indico_rag.indico.ranking import apply_ranking
from indico_rag.model import Attachment, Day, Event, Session, Talk
from indico_rag.tui.app import IndicoRagApp
from indico_rag.tui.filtering import material_state
from indico_rag.tui.screens.select import SelectScreen, talk_label

FIXTURES = Path(__file__).parent / "fixtures"


def _event() -> Event:
    pdf = Attachment(id="a1", filename="deck.pdf", kind="file", url="u")
    pptx = Attachment(id="a2", filename="deck.pptx", kind="file", url="u")
    t1 = Talk(id="c1", contribution_id="1", title="Neutrino Mass Limits",
              start=datetime(2026, 8, 31, 9, 0), end=None, speakers=["Li Chen"],
              track="Neutrinos", session_title="Plenary", attachments=[pdf], chosen=pdf)
    t2 = Talk(id="c2", contribution_id="2", title="Background Modeling",
              start=datetime(2026, 8, 31, 11, 0), end=None, speakers=["A. Müller"],
              track="Backgrounds", session_title="Plenary", attachments=[pptx], chosen=pptx)
    t3 = Talk(id="c3", contribution_id="3", title="Discussion", start=None, end=None,
              session_title="Plenary")
    return Event(id="1", host="h", title="E", slug="e",
                 days=[Day(date=date(2026, 8, 31),
                           sessions=[Session(id="s1", title="Plenary", slug="p",
                                             talks=[t1, t2, t3])])])


def test_talk_label_shows_time_title_speaker_and_file():
    talk = _event().all_talks()[0]
    label = talk_label(talk, selected=False)
    assert "09:00" in label and "Neutrino Mass Limits" in label
    assert "Li Chen" in label and "deck.pdf" in label


def test_talk_label_marks_conversion_and_absence():
    talks = _event().all_talks()
    assert "→ pdf" in talk_label(talks[1], selected=False)
    assert "no material" in talk_label(talks[2], selected=False)


def test_talk_label_shows_the_checkbox_state():
    talk = _event().all_talks()[0]
    assert talk_label(talk, selected=True) != talk_label(talk, selected=False)


async def test_select_all_then_none(tmp_path):
    app = IndicoRagApp()
    async with app.run_test() as pilot:
        screen = SelectScreen(_event(), tmp_path)
        await app.push_screen(screen)
        await pilot.pause()

        await pilot.press("a")
        assert screen.state.selected == {"1", "2", "3"}

        await pilot.press("N")
        assert screen.state.selected == set()


async def test_filtering_then_selecting_all_only_takes_the_visible_talks(tmp_path):
    app = IndicoRagApp()
    async with app.run_test() as pilot:
        screen = SelectScreen(_event(), tmp_path)
        await app.push_screen(screen)
        await pilot.pause()

        screen.spec.text = "neutrino"
        screen.refresh_tree()
        await pilot.pause()

        await pilot.press("a")
        assert screen.state.selected == {"1"}

        screen.spec.text = ""
        screen.refresh_tree()
        await pilot.pause()
        assert screen.state.selected == {"1"}, "clearing the filter must not clear the selection"


async def test_an_invalid_regex_is_reported_and_shows_nothing(tmp_path):
    app = IndicoRagApp()
    async with app.run_test() as pilot:
        screen = SelectScreen(_event(), tmp_path)
        await app.push_screen(screen)
        await pilot.pause()

        screen.spec.regex = True
        screen.spec.text = "Back("
        screen.refresh_tree()
        await pilot.pause()

        assert screen.shown == []
        assert "invalid" in screen.status_text.lower()


async def test_an_invalid_regex_leaves_the_previous_selection_intact(tmp_path):
    app = IndicoRagApp()
    async with app.run_test() as pilot:
        screen = SelectScreen(_event(), tmp_path)
        await app.push_screen(screen)
        await pilot.pause()

        await pilot.press("a")
        assert screen.state.selected == {"1", "2", "3"}

        screen.spec.regex = True
        screen.spec.text = "Back("
        screen.refresh_tree()
        await pilot.pause()

        assert screen.shown == []
        assert "invalid" in screen.status_text.lower()
        assert screen.state.selected == {"1", "2", "3"}, \
            "a typo in the filter must not clear a prior selection"


async def test_a_partially_selected_day_renders_as_partial(tmp_path):
    app = IndicoRagApp()
    async with app.run_test() as pilot:
        screen = SelectScreen(_event(), tmp_path)
        await app.push_screen(screen)
        await pilot.pause()

        tree = screen.query_one("#agenda")
        day_node = tree.root.children[0]
        assert str(day_node.label).startswith("[ ]")

        screen.state.toggle_talk("1")
        screen.refresh_tree()
        await pilot.pause()

        day_node = tree.root.children[0]
        label = str(day_node.label)
        assert label.startswith("[~]"), f"partially selected day must render [~], got {label!r}"
        assert not label.startswith("[ ]") and not label.startswith("[x]")

        screen.state.toggle_talk("2")
        screen.state.toggle_talk("3")
        screen.refresh_tree()
        await pilot.pause()

        day_node = tree.root.children[0]
        assert str(day_node.label).startswith("[x]")


async def test_action_override_cycles_attachments_then_skip_and_back(tmp_path):
    app = IndicoRagApp()
    async with app.run_test() as pilot:
        event = _event()
        talk = event.all_talks()[0]
        pdf = talk.attachments[0]
        other = Attachment(id="a3", filename="alt.pdf", kind="file", url="u2")
        talk.attachments.append(other)
        assert talk.chosen is pdf

        screen = SelectScreen(event, tmp_path)
        await app.push_screen(screen)
        await pilot.pause()

        tree = screen.query_one("#agenda")
        talk_node = tree.root.children[0].children[0].children[0]
        tree.move_cursor(talk_node)
        await pilot.pause()

        screen.action_override()
        assert talk.chosen is other
        talk_node = tree.root.children[0].children[0].children[0]
        assert "alt.pdf" in str(talk_node.label)

        screen.action_override()
        assert talk.chosen is None
        talk_node = tree.root.children[0].children[0].children[0]
        assert "no material" in str(talk_node.label)

        screen.action_override()
        assert talk.chosen is pdf
        talk_node = tree.root.children[0].children[0].children[0]
        assert "deck.pdf" in str(talk_node.label)


async def test_the_captured_event_renders_and_select_visible_matches_measured_material(tmp_path):
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
    counts = Counter(material_state(t) for t in talks)
    assert counts["none"] == 1
    materialless = next(t for t in talks if material_state(t) == "none")
    assert materialless.contribution_id == "304612"
    assert materialless.title == "Announcements"

    app = IndicoRagApp()
    async with app.run_test() as pilot:
        screen = SelectScreen(event, tmp_path)
        await app.push_screen(screen)
        await pilot.pause()

        await pilot.press("a")

        # visible_talks applies no material filter by default, so the
        # materialless talk is shown and action_select_visible selects it
        # along with the other 99 — "select everything shown" includes it,
        # and the download stage records the materialless one separately.
        assert len(screen.state.selected) == 100
        assert screen.state.selected == {t.contribution_id for t in talks}
