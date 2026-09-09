"""Which talks are selected, independent of how they are displayed."""
from __future__ import annotations

from typing import Literal

from indico_rag.model import Day, Event, Session

TriState = Literal["none", "partial", "all"]


class SelectionState:
    """A set of selected contribution ids, with tri-state rollups for parents."""

    def __init__(self, event: Event) -> None:
        self.event = event
        self.selected: set[str] = set()

    @property
    def count(self) -> int:
        return len(self.selected)

    def is_selected(self, contribution_id: str) -> bool:
        return contribution_id in self.selected

    def toggle_talk(self, contribution_id: str) -> None:
        self.selected.symmetric_difference_update({contribution_id})

    def set_talks(self, contribution_ids: list[str] | set[str], on: bool) -> None:
        if on:
            self.selected.update(contribution_ids)
        else:
            self.selected.difference_update(contribution_ids)

    def _rollup(self, contribution_ids: list[str]) -> TriState:
        if not contribution_ids:
            return "none"
        chosen = sum(1 for cid in contribution_ids if cid in self.selected)
        if chosen == 0:
            return "none"
        return "all" if chosen == len(contribution_ids) else "partial"

    def session_state(self, session: Session) -> TriState:
        return self._rollup([t.contribution_id for t in session.talks])

    def day_state(self, day: Day) -> TriState:
        return self._rollup(
            [t.contribution_id for s in day.sessions for t in s.talks]
        )

    def toggle_session(self, session: Session) -> None:
        ids = [t.contribution_id for t in session.talks]
        self.set_talks(ids, self._rollup(ids) != "all")

    def toggle_day(self, day: Day) -> None:
        ids = [t.contribution_id for s in day.sessions for t in s.talks]
        self.set_talks(ids, self._rollup(ids) != "all")
