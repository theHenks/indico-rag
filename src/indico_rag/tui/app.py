"""The Indico RAG terminal application."""
from __future__ import annotations

from pathlib import Path

from textual.app import App

from indico_rag import store
from indico_rag.model import Event
from indico_rag.tui.screens.home import HomeScreen
from indico_rag.tui.screens.new_event import NewEventScreen
from indico_rag.tui.screens.progress import ProgressScreen
from indico_rag.tui.screens.select import SelectScreen


class IndicoRagApp(App):
    """Root application: hosts the home, new-event and selection screens."""

    CSS = ""
    TITLE = "indico-rag"

    def __init__(self, state_root: Path | None = None) -> None:
        super().__init__()
        self.state_root = state_root
        self._config: store.Config | None = None

    @property
    def config(self) -> store.Config:
        """The stored configuration, read on first use.

        Reading it in the constructor would create the platform configuration
        directory for every application instance, including the many built by
        tests that never reach a screen needing it.
        """
        if self._config is None:
            self._config = store.load_config(self.state_root)
        return self._config

    def on_mount(self) -> None:
        self.push_screen(HomeScreen(self.state_root))

    def on_home_screen_new_scan(self, _: HomeScreen.NewScan) -> None:
        self.push_screen(NewEventScreen(self.state_root, self.config.default_host))

    def on_home_screen_open_scan(self, message: HomeScreen.OpenScan) -> None:
        event, record = store.load_scan(message.host, message.event_id, self.state_root)
        screen = SelectScreen(event, Path(record.output_dir or self.config.output_root))
        screen.state.set_talks(record.selection, True)
        self.push_screen(screen)

    def on_new_event_screen_scanned(self, message: NewEventScreen.Scanned) -> None:
        self.push_screen(SelectScreen(message.event, self.config.output_root))

    def on_select_screen_start(self, message: SelectScreen.Start) -> None:
        screen = self.screen
        event: Event = screen.event
        store.save_scan(event, selection=sorted(message.selected),
                        output_dir=str(message.output_root), root=self.state_root)
        self.push_screen(ProgressScreen(
            event, message.selected, message.output_root, self.config,
            token=store.get_token(event.host, self.state_root),
        ))
