import httpx
import pytest

from indico_rag.access.base import Regime, classify

INTERSTITIAL = '<html><script id="anubis_challenge" type="application/json">{}</script></html>'


def _response(status: int, text: str, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        text=text,
        headers=headers or {},
        request=httpx.Request("GET", "https://example.org/x"),
    )


def test_json_body_is_open():
    r = _response(200, '{"results": {}}', {"content-type": "application/json"})
    assert classify(r) is Regime.OPEN


def test_status_200_with_interstitial_is_proof_of_work_not_open():
    # The whole point: a status-code check would misread this as success.
    r = _response(200, INTERSTITIAL, {"content-type": "text/html"})
    assert classify(r) is Regime.PROOF_OF_WORK


@pytest.mark.parametrize("status", [401, 403])
def test_unauthorized_statuses_are_auth_required(status):
    r = _response(status, "denied", {"content-type": "text/html"})
    assert classify(r) is Regime.AUTH_REQUIRED


def test_html_login_page_is_auth_required():
    r = _response(
        200,
        '<html><form action="/login/" method="post">Please log in</form></html>',
        {"content-type": "text/html"},
    )
    assert classify(r) is Regime.AUTH_REQUIRED
