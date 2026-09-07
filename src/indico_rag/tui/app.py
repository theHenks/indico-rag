"""The Indico RAG terminal application."""
from __future__ import annotations

from pathlib import Path

from textual.app import App


class IndicoRagApp(App):
    """Root application: hosts the home, new-event and selection screens."""

    CSS = ""
    TITLE = "indico-rag"

    def __init__(self, state_root: Path | None = None) -> None:
        super().__init__()
        self.state_root = state_root
