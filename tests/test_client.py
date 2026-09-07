import httpx
import pytest
import respx

from indico_rag.access import anubis
from indico_rag.access.base import AuthRequired, Regime
from indico_rag.client import IndicoClient

HOST = "https://indico.example.org"

INTERSTITIAL = (
    '<html><script id="anubis_challenge" type="application/json">'
    '{"challenge": {"id": "chal-1", "randomData": "seed"}, '
    '"rules": {"difficulty": 2}}</script>'
    '<script id="anubis_base_prefix" type="application/json">""</script></html>'
)


@respx.mock
async def test_open_instance_returns_json_without_preparation():
    respx.get(f"{HOST}/export/timetable/1.json").mock(
        return_value=httpx.Response(200, json={"results": {"1": {}}})
    )
    async with IndicoClient(HOST) as client:
        data = await client.get_json("/export/timetable/1.json")
    assert data == {"results": {"1": {}}}
    assert client.regime is Regime.OPEN


@respx.mock
async def test_token_is_sent_as_a_bearer_header():
    route = respx.get(f"{HOST}/export/timetable/1.json").mock(
        return_value=httpx.Response(200, json={"results": {}})
    )
    async with IndicoClient(HOST, token="secret-token") as client:
        await client.get_json("/export/timetable/1.json")
    assert route.calls.last.request.headers["authorization"] == "Bearer secret-token"


@respx.mock
async def test_auth_required_raises_naming_the_host():
    respx.get(f"{HOST}/export/timetable/1.json").mock(return_value=httpx.Response(403))
    async with IndicoClient(HOST) as client:
        with pytest.raises(AuthRequired) as excinfo:
            await client.get_json("/export/timetable/1.json")
    assert excinfo.value.host == HOST


@respx.mock
async def test_challenge_is_solved_once_then_the_request_is_replayed():
    target = respx.get(f"{HOST}/export/timetable/1.json")
    target.side_effect = [
        httpx.Response(200, html=INTERSTITIAL),
        httpx.Response(200, json={"results": {"ok": True}}),
    ]
    passer = respx.get(
        url__startswith=f"{HOST}/.within.website/x/cmd/anubis/api/pass-challenge"
    ).mock(return_value=httpx.Response(200, html="<html>ok</html>"))

    async with IndicoClient(HOST) as client:
        data = await client.get_json("/export/timetable/1.json")

    assert data == {"results": {"ok": True}}
    assert client.regime is Regime.PROOF_OF_WORK
    assert passer.call_count == 1
    assert target.call_count == 2


@respx.mock
async def test_stream_to_recovers_when_the_file_url_is_challenged_once(tmp_path):
    pdf_bytes = b"%PDF-1.4 fake pdf content"
    respx.get(f"{HOST}/").mock(return_value=httpx.Response(200, json={"results": {}}))

    solved = {"done": False}

    def file_responder(request: httpx.Request) -> httpx.Response:
        if solved["done"]:
            return httpx.Response(
                200, content=pdf_bytes, headers={"content-type": "application/pdf"}
            )
        return httpx.Response(200, html=INTERSTITIAL)

    respx.get(f"{HOST}/files/paper.pdf").mock(side_effect=file_responder)

    def pass_responder(request: httpx.Request) -> httpx.Response:
        solved["done"] = True
        return httpx.Response(200, html="<html>ok</html>")

    passer = respx.get(
        url__startswith=f"{HOST}/.within.website/x/cmd/anubis/api/pass-challenge"
    ).mock(side_effect=pass_responder)

    dest = tmp_path / "paper.pdf"
    async with IndicoClient(HOST) as client:
        written = await client.stream_to("/files/paper.pdf", dest)

    assert written == len(pdf_bytes)
    assert dest.read_bytes() == pdf_bytes
    assert passer.call_count == 1


@respx.mock
async def test_stream_to_raises_rather_than_writing_a_challenge_page(tmp_path):
    respx.get(f"{HOST}/").mock(return_value=httpx.Response(200, json={"results": {}}))
    respx.get(f"{HOST}/files/paper.pdf").mock(
        return_value=httpx.Response(200, html=INTERSTITIAL)
    )
    respx.get(
        url__startswith=f"{HOST}/.within.website/x/cmd/anubis/api/pass-challenge"
    ).mock(return_value=httpx.Response(200, html="<html>ok</html>"))

    dest = tmp_path / "paper.pdf"
    async with IndicoClient(HOST) as client:
        with pytest.raises(anubis.ChallengeParseError):
            await client.stream_to("/files/paper.pdf", dest)

    assert not dest.exists() or dest.stat().st_size == 0


@respx.mock
async def test_stream_to_writes_a_genuine_html_attachment(tmp_path):
    html_body = b"<html><body><h1>Slides</h1></body></html>"
    respx.get(f"{HOST}/").mock(return_value=httpx.Response(200, json={"results": {}}))
    respx.get(f"{HOST}/files/slides.html").mock(
        return_value=httpx.Response(
            200, content=html_body, headers={"content-type": "text/html"}
        )
    )

    dest = tmp_path / "slides.html"
    async with IndicoClient(HOST) as client:
        written = await client.stream_to("/files/slides.html", dest)

    assert written == len(html_body)
    assert dest.read_bytes() == html_body


@respx.mock
async def test_stream_to_writes_a_payload_larger_than_one_chunk(tmp_path):
    # Past the 65536-byte chunk size, so the async-for continuation past the
    # first peeked chunk actually runs.
    body = bytes(range(256)) * 300
    respx.get(f"{HOST}/").mock(return_value=httpx.Response(200, json={"results": {}}))
    respx.get(f"{HOST}/files/big.pdf").mock(
        return_value=httpx.Response(
            200, content=body, headers={"content-type": "application/pdf"}
        )
    )

    dest = tmp_path / "big.pdf"
    async with IndicoClient(HOST) as client:
        written = await client.stream_to("/files/big.pdf", dest)

    assert written == len(body)
    assert dest.read_bytes() == body


@respx.mock
async def test_stream_to_handles_an_empty_body(tmp_path):
    respx.get(f"{HOST}/").mock(return_value=httpx.Response(200, json={"results": {}}))
    respx.get(f"{HOST}/files/empty.pdf").mock(
        return_value=httpx.Response(
            200, content=b"", headers={"content-type": "application/pdf"}
        )
    )

    dest = tmp_path / "empty.pdf"
    async with IndicoClient(HOST) as client:
        written = await client.stream_to("/files/empty.pdf", dest)

    assert written == 0
    assert dest.exists() and dest.read_bytes() == b""
