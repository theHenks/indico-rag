import json

import httpx
import pytest
import respx

from indico_rag.access import anubis
from indico_rag.access.base import AuthRequired, Regime, UnexpectedResponse
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
async def test_get_json_raises_unexpected_response_for_a_non_json_error_body():
    respx.get(f"{HOST}/export/timetable/1.json").mock(
        return_value=httpx.Response(400, html="<html>Bad Request</html>")
    )
    async with IndicoClient(HOST) as client:
        with pytest.raises(UnexpectedResponse) as excinfo:
            await client.get_json("/export/timetable/1.json")
    assert not isinstance(excinfo.value, json.JSONDecodeError)
    assert "400" in str(excinfo.value)
    assert "Bad Request" in str(excinfo.value)


@respx.mock
async def test_insufficient_scope_token_falls_back_to_anonymous_and_warns():
    calls = {"with_auth": 0, "without_auth": 0}

    def responder(request: httpx.Request) -> httpx.Response:
        if "authorization" in request.headers:
            calls["with_auth"] += 1
            return httpx.Response(
                400,
                json={
                    "error": "insufficient_scope",
                    "error_description": (
                        "The request requires higher privileges than provided "
                        "by the access token"
                    ),
                },
            )
        calls["without_auth"] += 1
        return httpx.Response(200, json={"results": {"ok": True}})

    respx.get(f"{HOST}/export/timetable/1.json").mock(side_effect=responder)

    async with IndicoClient(HOST, token="indp_badscope") as client:
        data = await client.get_json("/export/timetable/1.json")

    assert data == {"results": {"ok": True}}
    # Exactly one anonymous retry per request, never a repeated fallback loop.
    assert calls == {"with_auth": 1, "without_auth": 1}
    assert client.warning is not None
    assert "Classic API" in client.warning
    assert f"{HOST}/user/tokens/" in client.warning


@respx.mock
async def test_non_scope_400_raises_unexpected_response_without_stripping_the_token():
    route = respx.get(f"{HOST}/export/timetable/1.json").mock(
        return_value=httpx.Response(400, json={"error": "bad_request"})
    )
    async with IndicoClient(HOST, token="indp_x") as client:
        with pytest.raises(UnexpectedResponse):
            await client.get_json("/export/timetable/1.json")
    assert route.call_count == 1
    assert route.calls.last.request.headers["authorization"] == "Bearer indp_x"


@respx.mock
async def test_insufficient_scope_when_anonymous_retry_also_fails_raises_auth_required():
    respx.get(f"{HOST}/export/timetable/1.json").mock(
        return_value=httpx.Response(400, json={"error": "insufficient_scope"})
    )
    async with IndicoClient(HOST, token="indp_badscope") as client:
        with pytest.raises(AuthRequired) as excinfo:
            await client.get_json("/export/timetable/1.json")
    assert f"{HOST}/user/tokens/" in str(excinfo.value)
    assert "scope" in str(excinfo.value).lower()


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
async def test_stream_to_succeeds_when_the_homepage_carries_a_login_link(tmp_path):
    # The regression this whole fix is about: a normal homepage's nav links to
    # /login/, which must not make _ensure_prepared's root probe raise
    # AuthRequired for every host.
    pdf_bytes = b"%PDF-1.4 fake pdf content"
    respx.get(f"{HOST}/").mock(
        return_value=httpx.Response(
            200,
            html=(
                f'<html><body><nav><a href="{HOST}/login/">Login</a></nav>'
                "<h1>Welcome</h1></body></html>"
            ),
        )
    )
    respx.get(f"{HOST}/files/paper.pdf").mock(
        return_value=httpx.Response(
            200, content=pdf_bytes, headers={"content-type": "application/pdf"}
        )
    )

    dest = tmp_path / "paper.pdf"
    async with IndicoClient(HOST) as client:
        written = await client.stream_to("/files/paper.pdf", dest)

    assert written == len(pdf_bytes)
    assert dest.read_bytes() == pdf_bytes
    assert client.regime is Regime.OPEN


@respx.mock
async def test_stream_to_raises_when_the_file_url_redirects_to_login(tmp_path):
    # A per-file permission gate a host-root probe cannot see: the host is
    # OPEN, but this specific attachment redirects to a login page. Two real
    # mocked hops (not a hand-built `history=`) so httpx follows the redirect
    # itself and populates `response.history` the way it would live.
    respx.get(f"{HOST}/").mock(return_value=httpx.Response(200, json={"results": {}}))
    respx.get(f"{HOST}/files/secret.pdf").mock(
        return_value=httpx.Response(
            302, headers={"location": f"{HOST}/login/?next=/files/secret.pdf"}
        )
    )
    respx.get(f"{HOST}/login/", params={"next": "/files/secret.pdf"}).mock(
        return_value=httpx.Response(200, html="<html>please log in</html>")
    )

    dest = tmp_path / "secret.pdf"
    async with IndicoClient(HOST) as client:
        with pytest.raises(AuthRequired):
            await client.stream_to("/files/secret.pdf", dest)

    assert not dest.exists() or dest.stat().st_size == 0


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
