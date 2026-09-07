import httpx
import pytest
import respx

from indico_rag.access.base import AuthRequired, Regime
from indico_rag.client import IndicoClient

HOST = "https://indico.example.org"


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
    interstitial = (
        '<html><script id="anubis_challenge" type="application/json">'
        '{"challenge": {"id": "chal-1", "randomData": "seed"}, '
        '"rules": {"difficulty": 2}}</script>'
        '<script id="anubis_base_prefix" type="application/json">""</script></html>'
    )
    target = respx.get(f"{HOST}/export/timetable/1.json")
    target.side_effect = [
        httpx.Response(200, html=interstitial),
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
