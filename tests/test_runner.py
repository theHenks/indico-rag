import asyncio
import json
from datetime import date, datetime
from pathlib import Path

import httpx
import respx

from indico_rag.model import Attachment, Day, Event, Session, Talk
from indico_rag.pipeline import runner
from indico_rag.pipeline.manifest import MANIFEST_NAME, Manifest
from indico_rag.pipeline.runner import build_jobs, run_jobs
from indico_rag.store import Config

HOST = "https://agenda.infn.it"
FIXTURES = Path(__file__).parent / "fixtures"


def _event(*specs) -> Event:
    talks = []
    for index, (title, filename) in enumerate(specs, start=1):
        attachment = None
        if filename:
            attachment = Attachment(id=f"a{index}", filename=filename, kind="file",
                                    url=f"{HOST}/files/{filename}")
        talks.append(Talk(id=f"c{index}", contribution_id=str(index), title=title,
                          start=datetime(2026, 8, 31, 9 + index, 0), end=None,
                          speakers=["Maria Rossi"], session_title="Plenary",
                          attachments=[attachment] if attachment else [],
                          chosen=attachment))
    session = Session(id="s1", title="Plenary", slug="plenary", talks=talks)
    return Event(id="50058", host=HOST, title="Workshop", slug="workshop",
                 days=[Day(date=date(2026, 8, 31), sessions=[session])])


def _config(tmp_path) -> Config:
    return Config(output_root=tmp_path, download_concurrency=4, convert_concurrency=2,
                  extract_text=False)


def _mock_root() -> None:
    # IndicoClient.stream_to detects the host's access regime on "/" before the
    # first real file request; every test that lets run_jobs actually fetch
    # something needs this route mocked too, or respx has nothing to answer.
    respx.get(f"{HOST}/").mock(return_value=httpx.Response(200, json={"results": {}}))


def test_build_jobs_separates_talks_without_material(tmp_path):
    event = _event(("A", "a.pdf"), ("B", None))
    jobs, no_material = build_jobs(event, {"1", "2"}, tmp_path)
    assert [j.talk.contribution_id for j in jobs] == ["1"]
    assert [t.contribution_id for t in no_material] == ["2"]


def test_build_jobs_honours_the_selection(tmp_path):
    event = _event(("A", "a.pdf"), ("B", "b.pdf"))
    jobs, _ = build_jobs(event, {"2"}, tmp_path)
    assert [j.talk.contribution_id for j in jobs] == ["2"]


def test_build_jobs_deduplicates_colliding_destinations(tmp_path):
    event = _event(("Same Title", "a.pdf"), ("Same Title", "b.pdf"))
    for talk in event.all_talks():
        talk.start = datetime(2026, 8, 31, 9, 0)
    jobs, _ = build_jobs(event, {"1", "2"}, tmp_path)
    assert jobs[0].dest != jobs[1].dest


def test_build_jobs_over_the_real_event_matches_the_measured_counts():
    """Ground truth for event 50058 ("NOW 2026"): 100 talks, of which 97
    resolve straight to a PDF and 2 need conversion (99 jobs total), and 1
    (contribution 304612, "Announcements") has no material."""
    from indico_rag.indico.parser import attachments_by_contribution, parse_timetable
    from indico_rag.indico.ranking import apply_ranking

    timetable = json.loads((FIXTURES / "timetable_50058.json").read_text())
    export = json.loads((FIXTURES / "event_50058_contributions.json").read_text())
    event = parse_timetable(
        timetable, host=HOST, event_id="50058", event_title="NOW 2026",
        attachments=attachments_by_contribution(export),
    )
    apply_ranking(event)

    selected = {t.contribution_id for t in event.all_talks()}
    jobs, no_material = build_jobs(event, selected, Path("/tmp/does-not-matter"))

    assert len(jobs) == 99
    assert len(no_material) == 1
    assert no_material[0].contribution_id == "304612"
    destinations = [job.dest for job in jobs]
    assert len(destinations) == len(set(destinations)), "two jobs share a destination"


@respx.mock
async def test_pdfs_are_downloaded_and_recorded(tmp_path):
    event = _event(("A", "a.pdf"), ("B", "b.pdf"))
    _mock_root()
    respx.get(f"{HOST}/files/a.pdf").mock(return_value=httpx.Response(200, content=b"%PDF-a"))
    respx.get(f"{HOST}/files/b.pdf").mock(return_value=httpx.Response(200, content=b"%PDF-b"))

    jobs, none = build_jobs(event, {"1", "2"}, tmp_path)
    summary = await run_jobs(event, jobs, none, config=_config(tmp_path), root=tmp_path)

    assert summary.done == 2 and summary.failed == 0
    assert all(job.dest.exists() for job in jobs)
    manifest = Manifest.load(tmp_path / "50058_workshop" / MANIFEST_NAME)
    assert manifest.counts()["done"] == 2
    record = manifest.get("1")
    assert record.speakers == ["Maria Rossi"] and record.session == "Plenary"


@respx.mock
async def test_a_non_pdf_is_converted_and_the_original_is_replaced(tmp_path, monkeypatch):
    event = _event(("A", "deck.pptx"))
    _mock_root()
    respx.get(f"{HOST}/files/deck.pptx").mock(return_value=httpx.Response(200, content=b"stub"))

    async def fake_convert(source: Path, *, profile_index=0, timeout=120.0) -> Path:
        produced = source.with_suffix(".pdf")
        produced.write_bytes(b"%PDF-converted")
        return produced

    monkeypatch.setattr(runner, "convert_to_pdf", fake_convert)

    jobs, none = build_jobs(event, {"1"}, tmp_path)
    summary = await run_jobs(event, jobs, none, config=_config(tmp_path), root=tmp_path)

    assert summary.done == 1
    manifest = Manifest.load(tmp_path / "50058_workshop" / MANIFEST_NAME)
    record = manifest.get("1")
    assert record.converted is True
    assert record.local_path.endswith(".pdf")
    assert Path(record.local_path).read_bytes() == b"%PDF-converted"


@respx.mock
async def test_one_failure_does_not_stop_the_others(tmp_path):
    event = _event(("A", "a.pdf"), ("B", "b.pdf"))
    _mock_root()
    respx.get(f"{HOST}/files/a.pdf").mock(return_value=httpx.Response(404))
    respx.get(f"{HOST}/files/b.pdf").mock(return_value=httpx.Response(200, content=b"%PDF-b"))

    jobs, none = build_jobs(event, {"1", "2"}, tmp_path)
    summary = await run_jobs(event, jobs, none, config=_config(tmp_path), root=tmp_path)

    assert summary.done == 1 and summary.failed == 1
    assert summary.failures and summary.failures[0][0] == "A"


@respx.mock
async def test_a_failed_conversion_keeps_the_original_and_records_the_reason(tmp_path, monkeypatch):
    event = _event(("A", "deck.pptx"))
    _mock_root()
    respx.get(f"{HOST}/files/deck.pptx").mock(return_value=httpx.Response(200, content=b"stub"))

    async def failing_convert(source: Path, *, profile_index=0, timeout=120.0) -> Path:
        raise runner.ConversionError("LibreOffice exited 1")

    monkeypatch.setattr(runner, "convert_to_pdf", failing_convert)

    jobs, none = build_jobs(event, {"1"}, tmp_path)
    summary = await run_jobs(event, jobs, none, config=_config(tmp_path), root=tmp_path)

    assert summary.failed == 1
    record = Manifest.load(tmp_path / "50058_workshop" / MANIFEST_NAME).get("1")
    assert record.converted is False
    assert "LibreOffice exited 1" in record.conversion_error
    assert Path(record.local_path).suffix == ".pptx", "the original must be kept"


@respx.mock
async def test_an_unexpected_conversion_error_does_not_abort_the_other_talks(tmp_path, monkeypatch):
    """asyncio.TaskGroup cancels every sibling task the instant one raises.
    An error the conversion stage does not specifically recognise (anything
    other than ConversionError/OSError) must still be isolated to its own
    talk rather than escape _process and cancel talk B."""
    event = _event(("A", "deck.pptx"), ("B", "b.pdf"))
    _mock_root()
    respx.get(f"{HOST}/files/deck.pptx").mock(return_value=httpx.Response(200, content=b"stub"))
    respx.get(f"{HOST}/files/b.pdf").mock(return_value=httpx.Response(200, content=b"%PDF-b"))

    async def surprising_convert(source: Path, *, profile_index=0, timeout=120.0) -> Path:
        raise RuntimeError("boom")

    monkeypatch.setattr(runner, "convert_to_pdf", surprising_convert)

    jobs, none = build_jobs(event, {"1", "2"}, tmp_path)
    summary = await run_jobs(event, jobs, none, config=_config(tmp_path), root=tmp_path)

    assert summary.done == 1 and summary.failed == 1
    assert any("unexpected error" in reason for _, reason in summary.failures)
    record_b = Manifest.load(tmp_path / "50058_workshop" / MANIFEST_NAME).get("2")
    assert record_b.status == "done"


@respx.mock
async def test_conversion_failures_never_hold_a_profile_index_concurrently(tmp_path, monkeypatch):
    """Forces more concurrent conversion failures than there are LibreOffice
    profiles. If a profile were leaked on the failure path, or two
    conversions ever held the same index at once, this would either deadlock
    (caught by the timeout) or trip the reuse assertion below."""
    event = _event(*[(f"T{i}", f"{i}.pptx") for i in range(4)])
    _mock_root()
    for i in range(4):
        respx.get(f"{HOST}/files/{i}.pptx").mock(return_value=httpx.Response(200, content=b"stub"))

    held: set[int] = set()

    async def failing_convert(source: Path, *, profile_index=0, timeout=120.0) -> Path:
        assert profile_index not in held, "two conversions held the same profile index"
        held.add(profile_index)
        try:
            await asyncio.sleep(0.02)
            raise runner.ConversionError("LibreOffice exited 1")
        finally:
            held.discard(profile_index)

    monkeypatch.setattr(runner, "convert_to_pdf", failing_convert)

    config = Config(output_root=tmp_path, download_concurrency=4, convert_concurrency=2,
                    extract_text=False)
    jobs, none = build_jobs(event, {str(i + 1) for i in range(4)}, tmp_path)
    summary = await asyncio.wait_for(
        run_jobs(event, jobs, none, config=config, root=tmp_path), timeout=5
    )

    assert summary.failed == 4


@respx.mock
async def test_a_second_run_skips_completed_talks(tmp_path):
    event = _event(("A", "a.pdf"))
    _mock_root()
    route = respx.get(f"{HOST}/files/a.pdf").mock(
        return_value=httpx.Response(200, content=b"%PDF-a")
    )
    jobs, none = build_jobs(event, {"1"}, tmp_path)
    await run_jobs(event, jobs, none, config=_config(tmp_path), root=tmp_path)

    jobs2, none2 = build_jobs(event, {"1"}, tmp_path)
    summary = await run_jobs(event, jobs2, none2, config=_config(tmp_path), root=tmp_path)

    assert summary.skipped == 1
    assert route.call_count == 1, "a completed talk must not be re-fetched"


@respx.mock
async def test_download_concurrency_is_respected(tmp_path):
    event = _event(*[(f"T{i}", f"{i}.pdf") for i in range(6)])
    _mock_root()
    live = {"now": 0, "peak": 0}

    async def slow(request):
        live["now"] += 1
        live["peak"] = max(live["peak"], live["now"])
        await asyncio.sleep(0.05)
        live["now"] -= 1
        return httpx.Response(200, content=b"%PDF")

    for index in range(6):
        respx.get(f"{HOST}/files/{index}.pdf").mock(side_effect=slow)

    config = Config(output_root=tmp_path, download_concurrency=2, convert_concurrency=1,
                    extract_text=False)
    jobs, none = build_jobs(event, {str(i + 1) for i in range(6)}, tmp_path)
    await run_jobs(event, jobs, none, config=config, root=tmp_path)

    assert live["peak"] <= 2


@respx.mock
async def test_talks_without_material_are_recorded_not_silently_dropped(tmp_path):
    event = _event(("A", None))
    jobs, none = build_jobs(event, {"1"}, tmp_path)
    summary = await run_jobs(event, jobs, none, config=_config(tmp_path), root=tmp_path)
    assert summary.no_material == 1
    assert Manifest.load(tmp_path / "50058_workshop" / MANIFEST_NAME).get("1").status == "no_material"
