import asyncio
import tempfile
from datetime import date, datetime
from pathlib import Path

import httpx
import respx

from indico_rag.model import Attachment, Day, Event, Session, Talk
from indico_rag.store import Config
from indico_rag.tui.app import IndicoRagApp
from indico_rag.tui.screens.progress import ProgressScreen

_HOST = "https://progress.test"


def _event(count: int = 3) -> Event:
    talks = []
    for index in range(count):
        attachment = Attachment(id=f"a{index}", filename=f"deck{index}.pdf", kind="file",
                                url=f"{_HOST}/f/{index}.pdf")
        talks.append(Talk(id=f"c{index}", contribution_id=str(index),
                          title=f"Talk [{index}]", start=datetime(2026, 8, 31, 9 + index, 0),
                          end=None, speakers=["Ada Byron"], session_title="S",
                          attachments=[attachment], chosen=attachment))
    session = Session(id="s", title="S", slug="s", talks=talks)
    return Event(id="1", host=_HOST, title="E", slug="e",
                 days=[Day(date=date(2026, 8, 31), sessions=[session])])


@respx.mock
async def test_a_download_run_fills_the_table_and_reaches_a_summary(tmp_path):
    """Every talk must reach a terminal stage and the files must land.

    The screen runs the pipeline inside a worker, so a mistake here shows as a
    blank screen that never finishes rather than as an error: this asserts the
    rows update through to completion, not merely that the screen mounts.
    """
    respx.get(f"{_HOST}/").mock(return_value=httpx.Response(200, json={}))
    for index in range(3):
        respx.get(f"{_HOST}/f/{index}.pdf").mock(return_value=httpx.Response(
            200, content=b"%PDF-1.4 x", headers={"content-type": "application/pdf"}
        ))

    out = tmp_path / "out"
    app = IndicoRagApp(state_root=tmp_path / "state")
    async with app.run_test() as pilot:
        screen = ProgressScreen(_event(), {"0", "1", "2"}, out,
                                Config(output_root=out, download_concurrency=2,
                                       convert_concurrency=1, extract_text=False))
        await app.push_screen(screen)
        for _ in range(60):
            await pilot.pause()
            if screen.summary is not None:
                break
            await asyncio.sleep(0.05)

        assert screen.summary is not None, "the run never finished"
        assert screen.summary.done == 3
        assert screen.query_one("#rows").row_count == 3
        assert set(screen.stages.values()) == {"done"}, screen.stages

    assert len(list(out.rglob("*.pdf"))) == 3


@respx.mock
async def test_the_bar_tracks_completion_and_h_returns_to_the_scan_list(tmp_path):
    """The bar must reach full, and there must be a way back out.

    A run that finishes leaves the screen with nothing else to do, so being
    unable to leave it strands the whole application.
    """
    from textual.widgets import ProgressBar

    from indico_rag.tui.screens.home import HomeScreen

    respx.get(f"{_HOST}/").mock(return_value=httpx.Response(200, json={}))
    for index in range(3):
        respx.get(f"{_HOST}/f/{index}.pdf").mock(return_value=httpx.Response(
            200, content=b"%PDF-1.4 x", headers={"content-type": "application/pdf"}
        ))

    out = tmp_path / "out"
    app = IndicoRagApp(state_root=tmp_path / "state")
    async with app.run_test() as pilot:
        screen = ProgressScreen(_event(), {"0", "1", "2"}, out,
                                Config(output_root=out, download_concurrency=2,
                                       convert_concurrency=1, extract_text=False))
        await app.push_screen(screen)
        for _ in range(60):
            await pilot.pause()
            if screen.summary is not None:
                break
            await asyncio.sleep(0.05)

        bar = screen.query_one("#bar", ProgressBar)
        assert bar.progress == bar.total, f"bar stopped at {bar.progress}/{bar.total}"

        await pilot.press("h")
        await pilot.pause()
        assert isinstance(app.screen, HomeScreen), type(app.screen).__name__


"""Progress screen for the indico-rag TUI."""

from indico_rag.pipeline.runner import Summary
from indico_rag.tui.screens.progress import summary_lines


def test_summary_lines_report_every_bucket():
    summary = Summary(done=3, failed=1, no_material=2, skipped=4,
                      failures=[("Background Modeling", "conversion failed: exited 1")])
    text = "\n".join(summary_lines(summary))
    assert "3" in text and "done" in text
    assert "no material" in text or "no-material" in text
    assert "Background Modeling" in text
    assert "conversion failed" in text


def test_a_clean_run_says_so():
    text = "\n".join(summary_lines(Summary(done=5)))
    assert "FAILED" not in text
