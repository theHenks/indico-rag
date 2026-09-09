"""CLI wiring for `fetch`, exercised end to end with a mocked network.

Never points at a live host: `scan_event` is swapped for a stub event and
every HTTP call `run_jobs` makes goes through respx.
"""
from __future__ import annotations

import stat
from datetime import date, datetime
from pathlib import Path

import httpx
import respx

from indico_rag import cli, store
from indico_rag.access.base import AuthRequired
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


@respx.mock
def test_scan_token_flag_stores_and_sends_a_bearer_header(tmp_path, monkeypatch, capsys):
    stored = []
    monkeypatch.setattr(
        cli.store, "load_config",
        lambda *a, **k: store.Config(output_root=tmp_path, download_concurrency=4,
                                     convert_concurrency=1, extract_text=False),
    )
    monkeypatch.setattr(cli.store, "save_scan", lambda *a, **k: None)
    monkeypatch.setattr(
        cli.store, "set_token",
        lambda host, token, *a, **k: stored.append((host, token)),
    )

    respx.get(f"{HOST}/").mock(return_value=httpx.Response(200, json={"results": {}}))
    timetable_route = respx.get(f"{HOST}/export/timetable/50058.json").mock(
        return_value=httpx.Response(200, json={"results": {}})
    )
    respx.get(f"{HOST}/export/event/50058.json").mock(
        return_value=httpx.Response(200, json={"results": []})
    )

    exit_code = cli.main(["scan", EVENT_URL, "--token", "secret-token"])

    assert exit_code == 0
    assert stored == [(HOST, "secret-token")]
    assert timetable_route.calls.last.request.headers["authorization"] == "Bearer secret-token"


def test_auth_required_with_no_stored_token_names_the_tokens_url_and_login_command(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(
        cli.store, "load_config",
        lambda *a, **k: store.Config(output_root=tmp_path, download_concurrency=4,
                                     convert_concurrency=1, extract_text=False),
    )
    monkeypatch.setattr(cli.store, "get_token", lambda host, *a, **k: None)

    async def raising_scan_event(host, event_id, *, token=None, progress=None):
        raise AuthRequired(host)

    monkeypatch.setattr(cli, "scan_event", raising_scan_event)

    exit_code = cli.main(["fetch", EVENT_URL, "-o", str(tmp_path)])

    err = capsys.readouterr().err
    assert exit_code == 3
    assert f"{HOST}/user/tokens/" in err
    assert "indico-rag login" in err


def test_login_stores_a_token_for_a_bare_host(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(
        cli.store, "set_token",
        lambda host, token, *a, **k: calls.append((host, token)),
    )
    monkeypatch.setattr(cli.store, "state_root", lambda *a, **k: Path("/fake/state/root"))

    exit_code = cli.main(["login", HOST, "--token", "indp_abc"])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert calls == [(HOST, "indp_abc")]
    assert HOST in out
    assert "credentials.json" in out


def test_login_accepts_an_event_url_and_extracts_its_host(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(
        cli.store, "set_token",
        lambda host, token, *a, **k: calls.append((host, token)),
    )
    monkeypatch.setattr(cli.store, "state_root", lambda *a, **k: Path("/fake/state/root"))

    exit_code = cli.main(["login", EVENT_URL, "--token", "indp_abc"])

    assert exit_code == 0
    assert calls == [(HOST, "indp_abc")]


def test_login_writes_credentials_file_with_owner_only_permissions(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.store, "state_root", lambda *a, **k: tmp_path)

    exit_code = cli.main(["login", HOST, "--token", "indp_abc"])

    assert exit_code == 0
    path = tmp_path / "credentials.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert store.get_token(HOST, root=tmp_path) == "indp_abc"
