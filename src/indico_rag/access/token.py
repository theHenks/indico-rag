"""Bearer-token preparation for authenticated Indico instances."""
from __future__ import annotations

import httpx


def apply_token(client: httpx.AsyncClient, token: str | None) -> None:
    """Attach an Indico personal API token to every request the client makes."""
    if token:
        client.headers["Authorization"] = f"Bearer {token}"
