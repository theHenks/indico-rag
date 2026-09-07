"""CLI wiring for `fetch`, exercised end to end with a mocked network.

Never points at a live host: `scan_event` is swapped for a stub event and
every HTTP call `run_jobs` makes goes through respx.
"""
from __future__ import annotations

from datetime import date, datetime

import httpx
import respx

from indico_rag import cli, store
from indico_rag.model import Attachment, Day, Event, Session, Talk

HOST = "https://agenda.infn.it"
EVENT_URL = f"{HOST}/event/50058/"


def _event() -> Event:
    pdf = Attachment(id="a1", filename="a.pdf", kind="file", url=f"{HOST}/files/a.pdf")
    deck = Attachment(id="a2", filename="deck.pptx", kind="file", url=f"{HOST}/files/deck.pptx")
    talks = [
        Talk(id="c1", contribution_id="1", title="A", start=datetime(2026, 8, 31, 9, 0),
            end=None, speakers=["Maria Rossi"], session_title="Plenary",
            attachments=[pdf], chosen=pdf),
        Talk(id="c2", contribution_id="2", title="B", start=datetime(2026, 8, 31, 10, 0),
            end=None, speakers=["Maria Rossi"], session_title="Plenary",
            attachments=[deck], chosen=deck),
        Talk(id="c3", contribution_id="3", title="Announcements",
            start=datetime(2026, 8, 31, 11, 0), end=None, speakers=[],
            session_title="Plenary", attachments=[], chosen=None),
    ]
    session = Session(id="s1", title="Plenary", slug="plenary", talks=talks)
    return Event(id="50058", host=HOST, title="Workshop", slug="workshop",
                 days=[Day(date=date(2026, 8, 31), sessions=[session])])


@respx.mock
def test_fetch_downloads_scans_and_records_without_touching_a_live_host(
    tmp_path, monkeypatch, capsys
):
    respx.get(f"{HOST}/").mock(return_value=httpx.Response(200, json={"results": {}}))
    respx.get(f"{HOST}/files/a.pdf").mock(return_value=httpx.Response(200, content=b"%PDF-a"))
    respx.get(f"{HOST}/files/deck.pptx").mock(return_value=httpx.Response(200, content=b"stub"))

    monkeypatch.setattr(
        cli.store, "load_config",
        lambda *a, **k: store.Config(output_root=tmp_path, download_concurrency=4,
                                     convert_concurrency=1, extract_text=False),
    )
    monkeypatch.setattr(cli.store, "get_token", lambda host, *a, **k: None)
    monkeypatch.setattr(cli.store, "save_scan", lambda *a, **k: None)

    async def fake_scan_event(host, event_id, *, token=None, progress=None):
        assert host == HOST and event_id == "50058"
        return _event()

    monkeypatch.setattr(cli, "scan_event", fake_scan_event)

    async def fake_convert(source, *, profile_index=0, timeout=120.0):
        produced = source.with_suffix(".pdf")
        produced.write_bytes(b"%PDF-converted")
        return produced

    monkeypatch.setattr("indico_rag.pipeline.runner.convert_to_pdf", fake_convert)

    exit_code = cli.main(["fetch", EVENT_URL, "-o", str(tmp_path)])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "done=2 failed=0 skipped=0 no-material=1" in out
    assert (tmp_path / "50058_workshop" / "_manifest.json").exists()


@respx.mock
def test_fetch_exits_non_zero_when_text_extraction_fails(tmp_path, monkeypatch, capsys):
    """A run whose PDFs all downloaded fine but whose sidecars did not must
    still tell a script driving this tool that --extract was not honoured."""
    pdf = Attachment(id="a1", filename="a.pdf", kind="file", url=f"{HOST}/files/a.pdf")
    talk = Talk(id="c1", contribution_id="1", title="A", start=datetime(2026, 8, 31, 9, 0),
               end=None, speakers=["Maria Rossi"], session_title="Plenary",
               attachments=[pdf], chosen=pdf)
    single_talk_event = Event(
        id="50058", host=HOST, title="Workshop", slug="workshop",
        days=[Day(date=date(2026, 8, 31),
                  sessions=[Session(id="s1", title="Plenary", slug="plenary", talks=[talk])])],
    )

    respx.get(f"{HOST}/").mock(return_value=httpx.Response(200, json={"results": {}}))
    respx.get(f"{HOST}/files/a.pdf").mock(return_value=httpx.Response(200, content=b"%PDF-a"))

    monkeypatch.setattr(
        cli.store, "load_config",
        lambda *a, **k: store.Config(output_root=tmp_path, download_concurrency=4,
                                     convert_concurrency=1, extract_text=False),
    )
    monkeypatch.setattr(cli.store, "get_token", lambda host, *a, **k: None)
    monkeypatch.setattr(cli.store, "save_scan", lambda *a, **k: None)

    async def fake_scan_event(host, event_id, *, token=None, progress=None):
        return single_talk_event

    monkeypatch.setattr(cli, "scan_event", fake_scan_event)

    def failing_sidecar(pdf_path):
        raise ValueError("no text layer")

    monkeypatch.setattr("indico_rag.pipeline.runner.write_sidecar", failing_sidecar)

    exit_code = cli.main(["fetch", EVENT_URL, "-o", str(tmp_path), "--extract"])

    out = capsys.readouterr().out
    assert exit_code != 0, "an unmet --extract request must not exit 0"
    assert "done=1 failed=0" in out
    assert "text extraction failed" in out.lower()


@respx.mock
def test_fetch_reports_a_bad_event_reference_without_making_any_request(tmp_path, monkeypatch):
    monkeypatch.setattr(
        cli.store, "load_config",
        lambda *a, **k: store.Config(output_root=tmp_path, download_concurrency=4,
                                     convert_concurrency=1, extract_text=False),
    )
    exit_code = cli.main(["fetch", "not-a-url"])
    assert exit_code == 2
