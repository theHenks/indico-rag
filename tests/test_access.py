import httpx
import pytest

from indico_rag.access.base import Regime, classify

INTERSTITIAL = '<html><script id="anubis_challenge" type="application/json">{}</script></html>'


def _response(status: int, text: str, headers: dict | None = None, *,
             url: str = "https://example.org/x",
             history: list[httpx.Response] | None = None) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        text=text,
        headers=headers or {},
        request=httpx.Request("GET", url),
        history=history or [],
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


def test_403_with_json_content_type_is_auth_required():
    r = _response(403, '{"error": "denied"}', {"content-type": "application/json"})
    assert classify(r) is Regime.AUTH_REQUIRED


def test_normal_homepage_with_a_login_link_is_open():
    # The regression: every Indico homepage carries a login link in its
    # header nav. A link is not evidence the page itself requires auth.
    r = _response(
        200,
        '<html><body><nav><a href="https://example.org/login/">Login</a></nav>'
        "<h1>Welcome to the conference</h1></body></html>",
        {"content-type": "text/html"},
    )
    assert classify(r) is Regime.OPEN


def test_login_form_body_without_a_redirect_is_open():
    # classify() no longer scans the body for login markup: a login form
    # can appear on a page served directly (status 200, no redirect), which
    # carries no reliable signal that the *requested* resource is gated.
    r = _response(
        200,
        '<html><form action="/login/" method="post">Please log in</form></html>',
        {"content-type": "text/html"},
    )
    assert classify(r) is Regime.OPEN


def test_redirect_history_through_login_is_auth_required_even_off_host():
    # CERN's OIDC shape: /user/dashboard/ -> /login/?next=... -> /login/cern-oidc/
    # -> auth.cern.ch/... . The final URL carries no /login segment at all;
    # the evidence lives only in the history chain.
    hop1 = _response(302, "", url="https://indico.example.org/user/dashboard/")
    hop2 = _response(302, "", url="https://indico.example.org/login/?next=/user/dashboard/")
    hop3 = _response(302, "", url="https://indico.example.org/login/cern-oidc/")
    r = _response(
        200,
        "<html>you are being redirected</html>",
        {"content-type": "text/html"},
        url="https://auth.example.org/realms/main/protocol/openid-connect/auth",
        history=[hop1, hop2, hop3],
    )
    assert classify(r) is Regime.AUTH_REQUIRED
