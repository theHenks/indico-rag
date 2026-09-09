"""Enter an event URL and watch the scan progress."""
from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.message import Message
from textual.screen import Screen
from textual.widgets import Footer, Header, Input, Log, Static

from indico_rag import store
from indico_rag.access.base import AuthRequired
from indico_rag.indico.endpoints import parse_event_ref
from indico_rag.model import Event
from indico_rag.scan import scan_event


class TokenPrompt(Screen[str | None]):
    """Offer to store a personal API token the first time a host is used.

    Public material needs no token, so the prompt is skippable: an empty
    answer proceeds anonymously and the host is not asked about again.
    """

    BINDINGS = [Binding("escape", "skip", "skip")]

    def __init__(self, host: str) -> None:
        super().__init__()
        self.host = host

    def compose(self) -> ComposeResult:
        yield Static(
            f"First time using {self.host}.\n\n"
            "A personal API token is only needed for material that requires "
            "logging in; public events work without one.\n"
            f"Create one at {self.host}/user/tokens/ with the "
            "\"Classic API (read only)\" scope ticked.\n\n"
            "Paste a token to store it, or press enter to continue anonymously."
        )
        yield Input(placeholder="indp_… (optional)", password=True, id="token")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)

    def action_skip(self) -> None:
        self.dismiss(None)


class NewEventScreen(Screen):
    """Accepts a pasted event URL or bare id and reports each scan stage."""

    class Scanned(Message):
        def __init__(self, event: Event) -> None:
            super().__init__()
            self.event = event

    def __init__(self, state_root: Path | None = None, default_host: str = "") -> None:
        super().__init__()
        self.state_root = state_root
        self.default_host = default_host

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Paste an event URL, or an event id if a default host is configured:")
        yield Input(placeholder="https://agenda.infn.it/event/50058/timetable/", id="url")
        yield Log(id="progress")
        yield Footer()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.run_worker(self._scan(event.value), exclusive=True)

    async def _scan(self, text: str) -> None:
        log = self.query_one("#progress", Log)
        try:
            host, event_id = parse_event_ref(text, self.default_host or None)
        except ValueError as exc:
            log.write_line(f"error: {exc}")
            return

        if not store.is_known_host(host, self.state_root):
            token = await self.app.push_screen_wait(TokenPrompt(host))
            if token:
                store.set_token(host, token, self.state_root)
                log.write_line(f"token stored for {host}")
            else:
                # Remembering the answer keeps the prompt to once per host.
                store.set_host_regime(host, "anonymous", self.state_root)

        log.write_line(f"scanning {host}/event/{event_id}")
        try:
            scanned = await scan_event(
                host, event_id, token=store.get_token(host, self.state_root),
                progress=log.write_line,
            )
        except AuthRequired as exc:
            log.write_line(f"error: {exc}")
            token = await self.app.push_screen_wait(TokenPrompt(host))
            if not token:
                return
            store.set_token(host, token, self.state_root)
            log.write_line("token stored; retrying")
            try:
                scanned = await scan_event(
                    host, event_id, token=token, progress=log.write_line,
                )
            except AuthRequired as retry_exc:
                log.write_line(f"error: {retry_exc}")
                return

        store.save_scan(scanned, root=self.state_root)
        self.post_message(self.Scanned(scanned))
