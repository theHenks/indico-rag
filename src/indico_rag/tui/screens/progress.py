"""Live per-talk progress and the closing summary."""
from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static

from indico_rag.model import Event
from indico_rag.pipeline.runner import Summary, build_jobs, run_jobs
from indico_rag.store import Config


def summary_lines(summary: Summary) -> list[str]:
    lines = [
        f"done {summary.done} · failed {summary.failed} · "
        f"skipped {summary.skipped} · no material {summary.no_material}"
    ]
    lines.extend(f"FAILED  {title}: {reason}" for title, reason in summary.failures)
    return lines


class ProgressScreen(Screen):
    """One row per talk. The failure list is the output that matters here."""

    def __init__(self, event: Event, selected: set[str], output_root: Path, config: Config,
                 token: str | None = None) -> None:
        super().__init__()
        self.event = event
        self.selected = selected
        self.output_root = output_root
        self.config = config
        self.token = token
        self.stages: dict[str, str] = {}
        self.summary: Summary | None = None
        self._rows: dict[str, str] = {}

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(id="rows")
        yield Static(id="summary")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#rows", DataTable)
        table.add_columns("stage", "talk", "detail")
        self.run_worker(self._run(), exclusive=True)

    def _update(self, contribution_id: str, stage: str, detail: str) -> None:
        self.stages[contribution_id] = stage
        table = self.query_one("#rows", DataTable)
        title = next(
            (t.title for t in self.event.all_talks() if t.contribution_id == contribution_id),
            contribution_id,
        )
        if contribution_id in self._rows:
            table.update_cell(self._rows[contribution_id], "stage", stage)
            table.update_cell(self._rows[contribution_id], "detail", detail)
        else:
            key = table.add_row(stage, title, detail, key=contribution_id)
            self._rows[contribution_id] = key

    async def _run(self) -> None:
        jobs, no_material = build_jobs(self.event, self.selected, self.output_root)
        self.summary = await run_jobs(
            self.event, jobs, no_material, config=self.config, root=self.output_root,
            token=self.token, on_update=self._update,
        )
        self.query_one("#summary", Static).update("\n".join(summary_lines(self.summary)))
