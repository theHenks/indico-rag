"""Past scans, and the way into a new one."""
from __future__ import annotations

from pathlib import Path

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.message import Message
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static

from indico_rag import store


class HomeScreen(Screen):
    """Lists cached scans; the only way in to an existing or a new one."""

    # A screen-level AUTO_FOCUS of None falls back to the app default ("*"),
    # which would auto-focus the DataTable. DataTable binds "enter" itself
    # (to select_cursor), so a focused table would swallow "enter" before it
    # reaches this screen's "open" binding; "" actually disables auto-focus.
    AUTO_FOCUS = ""

    BINDINGS = [
        Binding("enter", "open", "open"),
        Binding("n", "new", "new event"),
        Binding("r", "rescan", "rescan"),
        Binding("d", "forget", "forget"),
        Binding("q", "quit", "quit"),
    ]

    class OpenScan(Message):
        def __init__(self, host: str, event_id: str, rescan: bool = False) -> None:
            super().__init__()
            self.host = host
            self.event_id = event_id
            self.rescan = rescan

    class NewScan(Message):
        pass

    def __init__(self, state_root: Path | None = None) -> None:
        super().__init__()
        self.state_root = state_root
        self.records: list[store.ScanRecord] = []

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(id="scans", cursor_type="row")
        yield Static("press n for a new event", id="status")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#scans", DataTable)
        table.add_columns("scanned", "event", "talks", "dates", "title")
        self.reload()

    def on_screen_resume(self) -> None:
        """Re-read the scans whenever this screen comes back to the front.

        A scan made while this screen was buried would otherwise be absent from
        the list until the application is restarted.
        """
        self.reload()

    def reload(self) -> None:
        self.records = store.list_scans(self.state_root)
        table = self.query_one("#scans", DataTable)
        table.clear()
        for record in self.records:
            # DataTable parses str cells as Rich markup, which would swallow
            # a literal "[...]" in a conference title (e.g. "[rescheduled]")
            # as a style tag; wrapping in Text bypasses that parsing.
            table.add_row(record.fetched_at, record.event_id, str(record.talk_count),
                          f"{record.first_day}..{record.last_day}", Text(record.title))

    def _current(self) -> store.ScanRecord | None:
        if not self.records:
            return None
        table = self.query_one("#scans", DataTable)
        index = table.cursor_row
        return self.records[index] if 0 <= index < len(self.records) else None

    def action_open(self) -> None:
        record = self._current()
        if record is not None:
            self.post_message(self.OpenScan(record.host, record.event_id))

    def action_rescan(self) -> None:
        record = self._current()
        if record is not None:
            self.post_message(self.OpenScan(record.host, record.event_id, rescan=True))

    def action_new(self) -> None:
        self.post_message(self.NewScan())

    def action_forget(self) -> None:
        record = self._current()
        if record is not None:
            store.delete_scan(record.host, record.event_id, self.state_root)
            self.reload()
