"""The day / session / talk tree where talks are chosen."""
from __future__ import annotations

from pathlib import Path

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.message import Message
from textual.screen import Screen
from textual.widgets import Footer, Header, Input, Static, Tree

from indico_rag.model import Event, Talk
from indico_rag.tui.filtering import FilterSpec, InvalidPattern, material_state, visible_talks
from indico_rag.tui.selection import SelectionState

_BOX = {"none": "[ ]", "partial": "[~]", "all": "[x]"}


def talk_label(talk: Talk, selected: bool) -> str:
    clock = talk.start.strftime("%H:%M") if talk.start else "--:--"
    who = f" — {talk.speakers[0]}" if talk.speakers else ""
    state = material_state(talk)
    if state == "none":
        marker = "no material"
    elif state == "pdf":
        marker = talk.chosen.filename
    else:
        name = talk.chosen.filename if talk.chosen else "link"
        marker = f"{name} → pdf"
    return f"{_BOX['all' if selected else 'none']} {clock}  {talk.title}{who}  [{marker}]"


class SelectScreen(Screen):
    """Choose talks. Filtering narrows what is shown; it never alters the selection."""

    # Textual otherwise auto-focuses the first focusable widget (the filter
    # Input), which would swallow keys like "a" and "N" as text instead of
    # dispatching them to this screen's bindings. A screen-level AUTO_FOCUS
    # of None falls back to the app's default ("*"), so it must be "" here
    # to actually disable auto-focus.
    AUTO_FOCUS = ""

    BINDINGS = [
        Binding("space", "toggle", "toggle"),
        Binding("a", "select_visible", "select shown"),
        Binding("N", "clear", "clear"),
        Binding("i", "invert", "invert"),
        Binding("slash", "focus_filter", "filter"),
        Binding("ctrl+r", "toggle_regex", "regex"),
        Binding("o", "override", "override file"),
        Binding("d", "change_output", "output dir"),
        Binding("D", "start", "download"),
    ]

    class Start(Message):
        def __init__(self, selected: set[str], output_root: Path) -> None:
            super().__init__()
            self.selected = selected
            self.output_root = output_root

    def __init__(self, event: Event, output_root: Path) -> None:
        super().__init__()
        self.event = event
        self.state = SelectionState(event)
        self.spec = FilterSpec()
        self.output_root = output_root
        self.shown: list[Talk] = []
        self.status_text = ""

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield Input(placeholder="filter (ctrl+r for regex)", id="filter")
        yield Tree("agenda", id="agenda")
        yield Static(id="status")
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_tree()

    def on_input_changed(self, event: Input.Changed) -> None:
        self.spec.text = event.value
        self.refresh_tree()

    def refresh_tree(self) -> None:
        tree: Tree = self.query_one("#agenda", Tree)
        try:
            self.shown = visible_talks(self.event, self.spec)
            self.status_text = ""
        except InvalidPattern as exc:
            self.shown = []
            self.status_text = f"invalid regular expression: {exc}"

        shown_ids = {talk.contribution_id for talk in self.shown}
        tree.clear()
        tree.root.expand()

        for day in self.event.days:
            day_talks = [t for s in day.sessions for t in s.talks if t.contribution_id in shown_ids]
            if not day_talks:
                continue
            # Tree labels are parsed as Rich markup, and the tri-state boxes
            # contain literal brackets ("[x]"); wrapping in Text bypasses that
            # parsing so the brackets render as plain text.
            day_node = tree.root.add(
                Text(f"{_BOX[self.state.day_state(day)]} {day.date.isoformat()}"),
                data=("day", day), expand=True,
            )
            for session in day.sessions:
                session_talks = [t for t in session.talks if t.contribution_id in shown_ids]
                if not session_talks:
                    continue
                session_node = day_node.add(
                    Text(f"{_BOX[self.state.session_state(session)]} {session.title}"),
                    data=("session", session), expand=True,
                )
                for talk in session_talks:
                    session_node.add_leaf(
                        Text(talk_label(talk, self.state.is_selected(talk.contribution_id))),
                        data=("talk", talk),
                    )

        mode = "regex" if self.spec.regex else "substring"
        counts = f"{self.state.count} selected · {len(self.shown)} shown · filter: {mode}"
        self.query_one("#status", Static).update(self.status_text or counts)

    def _current(self) -> tuple[str, object] | None:
        node = self.query_one("#agenda", Tree).cursor_node
        return node.data if node is not None else None

    def action_toggle(self) -> None:
        current = self._current()
        if current is None:
            return
        kind, obj = current
        if kind == "talk":
            self.state.toggle_talk(obj.contribution_id)
        elif kind == "session":
            self.state.toggle_session(obj)
        elif kind == "day":
            self.state.toggle_day(obj)
        self.refresh_tree()

    def action_select_visible(self) -> None:
        self.state.set_talks([t.contribution_id for t in self.shown], True)
        self.refresh_tree()

    def action_clear(self) -> None:
        self.state.selected.clear()
        self.refresh_tree()

    def action_invert(self) -> None:
        for talk in self.shown:
            self.state.toggle_talk(talk.contribution_id)
        self.refresh_tree()

    def action_focus_filter(self) -> None:
        self.query_one("#filter", Input).focus()

    def action_toggle_regex(self) -> None:
        self.spec.regex = not self.spec.regex
        self.refresh_tree()

    def action_override(self) -> None:
        current = self._current()
        if current is None or current[0] != "talk":
            return
        talk: Talk = current[1]
        if len(talk.attachments) < 2:
            return
        order = talk.attachments + [None]
        index = order.index(talk.chosen) if talk.chosen in order else 0
        talk.chosen = order[(index + 1) % len(order)]
        self.refresh_tree()

    def action_change_output(self) -> None:
        self.app.push_screen(
            OutputPrompt(self.output_root), callback=self._set_output
        )

    def _set_output(self, path: Path | None) -> None:
        if path is not None:
            self.output_root = path
            self.refresh_tree()

    def action_start(self) -> None:
        self.post_message(self.Start(set(self.state.selected), self.output_root))


class OutputPrompt(Screen[Path | None]):
    """Ask for an output directory."""

    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def __init__(self, current: Path) -> None:
        super().__init__()
        self.current = current

    def compose(self) -> ComposeResult:
        yield Static("Output directory:")
        yield Input(value=str(self.current), id="path")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(Path(event.value).expanduser())

    def action_cancel(self) -> None:
        self.dismiss(None)
