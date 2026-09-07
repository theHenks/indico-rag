from datetime import date, datetime

from textual.coordinate import Coordinate
from textual.widgets import DataTable
from textual.widgets._data_table import default_cell_formatter

from indico_rag.access.base import AuthRequired
from indico_rag.model import Day, Event, Session, Talk
from indico_rag.store import load_scan, save_scan
from indico_rag.tui.app import IndicoRagApp
from indico_rag.tui.screens.home import HomeScreen
from indico_rag.tui.screens.new_event import NewEventScreen


def _event(event_id="50058", title="LNGS Workshop") -> Event:
    talk = Talk(id="c1", contribution_id="1", title="Welcome",
                start=datetime(2026, 8, 31, 9, 0), end=None)
    return Event(id=event_id, host="https://agenda.infn.it", title=title, slug="w",
                 days=[Day(date=date(2026, 8, 31),
                           sessions=[Session(id="s1", title="Plenary", slug="p", talks=[talk])])])


async def test_home_lists_saved_scans(tmp_path):
    save_scan(_event(), root=tmp_path)
    save_scan(_event(event_id="60000", title="Other Workshop"), root=tmp_path)

    app = IndicoRagApp(state_root=tmp_path)
    async with app.run_test() as pilot:
        screen = HomeScreen(state_root=tmp_path)
        await app.push_screen(screen)
        await pilot.pause()
        titles = [record.title for record in screen.records]
        assert "LNGS Workshop" in titles and "Other Workshop" in titles


async def test_home_is_empty_when_nothing_was_scanned(tmp_path):
    app = IndicoRagApp(state_root=tmp_path)
    async with app.run_test() as pilot:
        screen = HomeScreen(state_root=tmp_path)
        await app.push_screen(screen)
        await pilot.pause()
        assert screen.records == []


async def test_deleting_a_scan_removes_it_from_the_list(tmp_path):
    save_scan(_event(), root=tmp_path)
    app = IndicoRagApp(state_root=tmp_path)
    async with app.run_test() as pilot:
        screen = HomeScreen(state_root=tmp_path)
        await app.push_screen(screen)
        await pilot.pause()
        screen.action_forget()
        await pilot.pause()
        assert screen.records == []


async def test_empty_table_key_bindings_are_a_no_op(tmp_path):
    """The home screen is the first thing a fresh install shows; with no
    scans yet, enter/rescan/forget must not raise IndexError."""
    app = IndicoRagApp(state_root=tmp_path)
    async with app.run_test() as pilot:
        screen = HomeScreen(state_root=tmp_path)
        await app.push_screen(screen)
        await pilot.pause()

        assert screen._current() is None
        await pilot.press("enter")
        await pilot.press("r")
        await pilot.press("d")
        await pilot.pause()
        assert screen.records == []


async def test_deleting_the_highlighted_row_keeps_the_rest_consistent(tmp_path):
    save_scan(_event(event_id="10", title="First"), root=tmp_path)
    save_scan(_event(event_id="20", title="Second"), root=tmp_path)
    save_scan(_event(event_id="30", title="Third"), root=tmp_path)

    app = IndicoRagApp(state_root=tmp_path)
    async with app.run_test() as pilot:
        screen = HomeScreen(state_root=tmp_path)
        await app.push_screen(screen)
        await pilot.pause()

        assert len(screen.records) == 3
        middle_index = next(i for i, r in enumerate(screen.records) if r.event_id == "20")
        table = screen.query_one("#scans", DataTable)
        table.move_cursor(row=middle_index)
        await pilot.pause()

        screen.action_forget()
        await pilot.pause()

        remaining_ids = {r.event_id for r in screen.records}
        assert remaining_ids == {"10", "30"}

        for record in screen.records:
            event, _ = load_scan(record.host, record.event_id, tmp_path)
            assert event.id == record.event_id

        current = screen._current()
        assert current is not None
        assert current.event_id in remaining_ids


async def test_title_with_square_brackets_renders_intact(tmp_path):
    save_scan(_event(event_id="99", title="NOW 2026 [rescheduled]"), root=tmp_path)

    app = IndicoRagApp(state_root=tmp_path)
    async with app.run_test() as pilot:
        screen = HomeScreen(state_root=tmp_path)
        await app.push_screen(screen)
        await pilot.pause()

        table = screen.query_one("#scans", DataTable)
        cell = table.get_cell_at(Coordinate(0, 4))
        # DataTable renders a raw str cell through Rich markup parsing, which
        # would swallow "[rescheduled]" as a (invalid) style tag; run the
        # stored cell through the same formatter DataTable uses to render it,
        # to check what actually reaches the screen rather than raw storage.
        rendered = default_cell_formatter(cell)
        text = rendered.plain if hasattr(rendered, "plain") else str(rendered)
        assert "NOW 2026 [rescheduled]" in text


async def test_new_event_screen_reports_an_unparseable_reference(tmp_path):
    app = IndicoRagApp(state_root=tmp_path)
    async with app.run_test() as pilot:
        screen = NewEventScreen(state_root=tmp_path)
        await app.push_screen(screen)
        await pilot.pause()

        await screen._scan("not a url and not an id")
        await pilot.pause()

        log = screen.query_one("#progress")
        assert any("error" in line.lower() for line in log.lines)
        # The screen must stay usable: querying it again must not raise.
        assert screen.query_one("#url") is not None


async def test_new_event_screen_surfaces_auth_required_with_its_token_url(tmp_path, monkeypatch):
    async def fake_scan_event(host, event_id, *, token=None, progress=None):
        raise AuthRequired(host)

    monkeypatch.setattr("indico_rag.tui.screens.new_event.scan_event", fake_scan_event)

    app = IndicoRagApp(state_root=tmp_path)
    async with app.run_test() as pilot:
        screen = NewEventScreen(state_root=tmp_path)
        await app.push_screen(screen)
        await pilot.pause()

        await screen._scan("https://indico.example.org/event/123/")
        await pilot.pause()

        log = screen.query_one("#progress")
        assert any("https://indico.example.org/user/tokens/" in line for line in log.lines)
