"""Live per-talk progress and the closing summary."""
from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from rich.text import Text
from textual.widgets import DataTable, Footer, Header, Static
from textual.widgets.data_table import ColumnKey, RowKey

from indico_rag.model import Event
from indico_rag.pipeline.runner import Summary, build_jobs, run_jobs
from indico_rag.store import Config


def summary_lines(summary: Summary) -> list[str]:
    lines = [
        f"done {summary.done} · failed {summary.failed} · "
        f"skipped {summary.skipped} · no material {summary.no_material}"
    ]
    lines.extend(f"FAILED  {title}: {reason}" for title, reason in summary.failures)
    # A talk whose PDF is fine but whose text sidecar failed is reported apart
    # from an outright failure, because the download itself is usable.
    lines.extend(
        f"NO TEXT  {title}: {reason}" for title, reason in summary.extraction_failures
    )
    return lines


class ProgressScreen(Screen):
    """One row per talk. The failure list is the output that matters here."""

    BINDINGS = [
        Binding("escape", "leave", "back"),
        Binding("q", "leave", "back"),
    ]

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
        self._rows: dict[str, RowKey] = {}
        self._columns: list[ColumnKey] = []

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(id="rows")
        yield Static(id="summary")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#rows", DataTable)
        # add_columns returns the generated keys; the header strings are labels
        # and cannot be used to address a cell.
        self._columns = table.add_columns("stage", "talk", "detail")
        self._total = len(self.selected)
        self._finished = 0
        self._render_counts()
        self.run_worker(self._run(), exclusive=True)

    def action_leave(self) -> None:
        """Return to the previous screen, stopping the run if it is still going."""
        if self.summary is None:
            self.workers.cancel_all()
        self.dismiss(None)

    def _render_counts(self) -> None:
        self.query_one("#summary", Static).update(
            f"{self._finished}/{self._total} talks handled"
        )

    def _update(self, contribution_id: str, stage: str, detail: str) -> None:
        self.stages[contribution_id] = stage
        table = self.query_one("#rows", DataTable)
        title = next(
            (t.title for t in self.event.all_talks() if t.contribution_id == contribution_id),
            contribution_id,
        )
        stage_column, _, detail_column = self._columns
        if contribution_id in self._rows:
            row = self._rows[contribution_id]
            table.update_cell(row, stage_column, stage)
            table.update_cell(row, detail_column, detail)
        else:
            self._rows[contribution_id] = table.add_row(
                stage, Text(title), Text(detail), key=contribution_id
            )

        if stage in ("done", "failed", "skipped"):
            self._finished += 1
            self._render_counts()

    async def _run(self) -> None:
        """Run the download, reporting a failure on screen rather than dying quietly.

        A worker that raises leaves the screen blank with no indication of why,
        which is indistinguishable from a run that has simply stalled.
        """
        try:
            jobs, no_material = build_jobs(self.event, self.selected, self.output_root)
            self.summary = await run_jobs(
                self.event, jobs, no_material, config=self.config, root=self.output_root,
                token=self.token, on_update=self._update,
            )
        except Exception as exc:  # noqa: BLE001 - shown to the user, never swallowed
            self.query_one("#summary", Static).update(
                f"the download stopped: {type(exc).__name__}: {exc}"
            )
            raise

        self.query_one("#summary", Static).update("\n".join(summary_lines(self.summary)))
