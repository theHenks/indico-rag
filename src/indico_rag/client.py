"""One HTTP client that adapts to whichever access regime a host uses."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import TracebackType

import httpx

from indico_rag.access import anubis
from indico_rag.access.base import (
    AuthRequired,
    Regime,
    classify,
    looks_like_login,
    redirected_through_login,
)
from indico_rag.access.token import apply_token

USER_AGENT = "indico-rag/0.1 (+https://github.com/theHenks/indico-rag)"


class IndicoClient:
    """Fetch JSON and files from one Indico host, solving Anubis when required.

    A single cookie jar is shared by every concurrent request. When a response
    comes back as a fresh challenge, exactly one coroutine re-solves it while
    holding `_solve_lock`; the others wait and then retry.
    """

    def __init__(self, host: str, *, token: str | None = None, timeout: float = 60.0) -> None:
        self.host = host.rstrip("/")
        self.regime: Regime | None = None
        self._client = httpx.AsyncClient(
            base_url=self.host,
            follow_redirects=True,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT},
        )
        apply_token(self._client, token)
        self._solve_lock = asyncio.Lock()
        self._last_solved_at: float | None = None

    async def __aenter__(self) -> "IndicoClient":
        return self

    async def __aexit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                        tb: TracebackType | None) -> None:
        await self._client.aclose()

    async def get_json(self, path: str, params: dict | None = None) -> dict:
        response = await self._request("GET", path, params=params)
        return response.json()

    async def stream_to(self, url: str, dest: Path) -> int:
        """Stream a URL into `dest`, returning the number of bytes written."""
        await self._ensure_prepared(url)
        written = await self._stream_checked(url, dest)
        if written is not None:
            return written

        # The body was a challenge or a login page rather than the file. Anubis
        # answers with status 200, so writing it would leave HTML in a .pdf.
        await self._request("GET", url)
        written = await self._stream_checked(url, dest)
        if written is None:
            raise anubis.ChallengeParseError(
                f"{url} still answers with a challenge after a solved proof of work"
            )
        return written

    async def _stream_checked(self, url: str, dest: Path) -> int | None:
        """Stream `url` into `dest`, or return None if it is a login or challenge page.

        A redirect through a login page is checked regardless of content type.
        Otherwise, only HTML bodies are inspected: an attachment may legitimately
        be an HTML file, and a real one carries none of Anubis's markers.
        """
        async with self._client.stream("GET", url) as response:
            response.raise_for_status()
            if redirected_through_login(response):
                return None

            chunks = response.aiter_bytes(chunk_size=65536)
            try:
                first = await anext(chunks)
            except StopAsyncIteration:
                first = b""

            if "html" in response.headers.get("content-type", ""):
                text = first.decode("utf-8", errors="replace")
                if anubis.is_interstitial(text) or looks_like_login(text):
                    return None

            written = 0
            with dest.open("wb") as handle:
                if first:
                    handle.write(first)
                    written += len(first)
                async for chunk in chunks:
                    handle.write(chunk)
                    written += len(chunk)
        return written

    async def _ensure_prepared(self, url: str) -> None:
        # Detect on the host root, never on `url`: probing the file URL would
        # fetch the file once here and again in the stream below.
        if self.regime is None:
            await self._request("GET", "/")

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        issued_at = time.monotonic()
        response = await self._client.request(method, path, **kwargs)
        regime = classify(response)
        self.regime = regime

        if regime is Regime.AUTH_REQUIRED:
            raise AuthRequired(self.host)

        if regime is Regime.PROOF_OF_WORK:
            await self._solve(response, issued_at)
            response = await self._client.request(method, path, **kwargs)
            regime = classify(response)
            if regime is Regime.AUTH_REQUIRED:
                raise AuthRequired(self.host)
            if regime is Regime.PROOF_OF_WORK:
                raise anubis.ChallengeParseError(
                    f"{self.host} still challenges after a solved proof of work"
                )
            self.regime = Regime.PROOF_OF_WORK

        return response

    async def _solve(self, response: httpx.Response, issued_at: float) -> None:
        """Solve the Anubis challenge in `response`, unless it is already stale.

        `issued_at` is when the request that produced `response` was sent. A
        challenge is server-wide: once any request solves one, the shared
        cookie jar authenticates every pending request, so a challenge issued
        before the last solve is answered without redoing the proof of work.
        """
        async with self._solve_lock:
            if self._last_solved_at is not None and self._last_solved_at > issued_at:
                return

            challenge = anubis.parse_challenge(response.text)
            started = time.monotonic()
            digest, nonce = await asyncio.to_thread(
                anubis.solve, challenge.challenge, challenge.difficulty
            )
            elapsed_ms = int((time.monotonic() - started) * 1000)

            await self._client.get(
                f"{challenge.base_prefix}/.within.website/x/cmd/anubis/api/pass-challenge",
                params={
                    "id": challenge.challenge_id,
                    "response": digest,
                    "nonce": nonce,
                    "redir": str(response.url),
                    "elapsedTime": elapsed_ms,
                },
            )
            self._last_solved_at = time.monotonic()
