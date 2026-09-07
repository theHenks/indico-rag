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


def classify(response: httpx.Response) -> Regime:
    if response.status_code in (401, 403):
        return Regime.AUTH_REQUIRED

    content_type = response.headers.get("content-type", "")
    if "json" in content_type:
        return Regime.OPEN

    body = response.text
    if is_interstitial(body):
        return Regime.PROOF_OF_WORK
    if _LOGIN_RE.search(body):
        return Regime.AUTH_REQUIRED
    return Regime.OPEN
