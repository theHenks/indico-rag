"""Classification of an Indico host's access regime."""
from __future__ import annotations

import re
from enum import StrEnum

import httpx

from indico_rag.access.anubis import is_interstitial

_LOGIN_RE = re.compile(r"""(action|href)=["'][^"']*/login""", re.I)


class Regime(StrEnum):
    OPEN = "open"
    PROOF_OF_WORK = "proof_of_work"
    AUTH_REQUIRED = "auth_required"


class AuthRequired(Exception):
    """The host needs an Indico API token that is absent or was rejected."""

    def __init__(self, host: str) -> None:
        super().__init__(
            f"{host} requires authentication; create a token at {host}/user/tokens/"
        )
        self.host = host


def looks_like_login(text: str) -> bool:
    """Report whether `text` is itself the body of a login page.

    Valid only when an HTML body arrives in place of a specific resource that
    was expected to be something else — an attachment download, or a re-fetch
    after solving an Anubis challenge. There, any login form or link is strong
    evidence the server substituted a login page for that resource. It is not
    a safe signal for a general page fetch: an ordinary page (a host's
    homepage, say) legitimately links to a login page from its navigation
    without itself requiring authentication, which is what `classify` must
    not conflate with an actual login redirect.
    """
    return _LOGIN_RE.search(text) is not None


def _has_login_segment(url: httpx.URL) -> bool:
    return "login" in url.path.strip("/").split("/")


def redirected_through_login(response: httpx.Response) -> bool:
    """Report whether `response`'s final URL or any URL it redirected through has a `/login` path segment.

    A protected page redirects to a login page before landing anywhere else.
    Some login flows (CERN's OIDC, for example) then redirect again to an
    identity provider on a different host whose URL has no `/login` segment,
    so `response.url` alone can miss the evidence; the redirect chain in
    `response.history` is where it survives.
    """
    urls = (response.url, *(prior.url for prior in response.history))
    return any(_has_login_segment(url) for url in urls)


def classify(response: httpx.Response) -> Regime:
    if response.status_code in (401, 403):
        return Regime.AUTH_REQUIRED

    if redirected_through_login(response):
        return Regime.AUTH_REQUIRED

    content_type = response.headers.get("content-type", "")
    if "json" in content_type:
        return Regime.OPEN

    if is_interstitial(response.text):
        return Regime.PROOF_OF_WORK

    return Regime.OPEN
