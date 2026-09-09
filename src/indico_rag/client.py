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
    UnexpectedResponse,
    classify,
    looks_like_login,
    redirected_through_login,
)
from indico_rag.access.token import apply_token

USER_AGENT = "indico-rag/0.1 (+https://github.com/theHenks/indico-rag)"

# A challenge answered by one download can be superseded by another still in
# flight, so a stream gets a few attempts before the talk is called failed.
_STREAM_ATTEMPTS = 6
_STREAM_BACKOFF = 1.0


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
        self._prepare_lock = asyncio.Lock()
        # Closed while a challenge is being answered. Any request issued in
        # that window draws a challenge of its own, and Anubis tracks only one
        # outstanding challenge per client, so the answer being computed would
        # stop matching the cookie and the whole run would re-challenge itself.
        self._open = asyncio.Event()
        self._open.set()
        self._last_solved_at: float | None = None
        self.warning: str | None = None

    async def __aenter__(self) -> "IndicoClient":
        return self

    async def __aexit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                        tb: TracebackType | None) -> None:
        await self._client.aclose()

    async def get_json(self, path: str, params: dict | None = None) -> dict:
        response = await self._request("GET", path, params=params)
        if response.status_code >= 400 or "json" not in response.headers.get("content-type", ""):
            raise UnexpectedResponse(path, response)
        return response.json()

    def _drop_token(self) -> None:
        """Stop sending the token, recording why the first time.

        A token the host rejects for lack of scope must not leave the tool less
        capable than sending none at all: anonymous access is a supported mode
        and reaches all public material. The warning names the missing scope so
        the token can be corrected rather than silently tolerated.
        """
        self._client.headers.pop("Authorization", None)
        if self.warning is None:
            self.warning = (
                f"the stored token for {self.host} lacks the Classic API (read only) "
                f"scope and is being ignored; add that scope at "
                f"{self.host}/user/tokens/ to use it"
            )

    async def stream_to(self, url: str, dest: Path) -> int:
        """Stream a URL into `dest`, returning the number of bytes written.

        A challenge met mid-run is treated as transient. Downloads run in
        parallel and each is challenged on its own, so one being answered while
        others are still in flight can leave a request holding a challenge that
        no longer matches the cookie. The auth cookie is host-wide and long
        lived, so the answer is to let another attempt pick up whichever solve
        lands first rather than to fail the talk.
        """
        await self._ensure_prepared(url)

        last_error: Exception | None = None
        for attempt in range(_STREAM_ATTEMPTS):
            written = await self._stream_checked(url, dest)
            if written is not None:
                return written

            # The body was a challenge or a login page rather than the file.
            # Anubis answers with status 200, so writing it would leave HTML in
            # a .pdf.
            try:
                await self._request("GET", url)
            except anubis.ChallengeParseError as exc:
                last_error = exc
            if attempt + 1 < _STREAM_ATTEMPTS:
                await asyncio.sleep(_STREAM_BACKOFF * (attempt + 1))

        raise anubis.ChallengeParseError(
            f"{url} still answers with a challenge after {_STREAM_ATTEMPTS} attempts"
        ) from last_error

    async def _stream_checked(self, url: str, dest: Path) -> int | None:  # noqa: C901
        """Stream `url` into `dest`, or return None if it is a login or challenge page.

        A redirect through a login page is checked regardless of content type.
        Otherwise, only HTML bodies are inspected: an attachment may legitimately
        be an HTML file, and a real one carries none of Anubis's markers.
        """
        await self._open.wait()
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
        """Obtain the host's challenge cookie once, before concurrent work starts.

        The probe asks for the first byte of `url` rather than the host root:
        a root page is often served without a challenge, so probing it settles
        the regime while leaving the cookie jar empty, and every later download
        is then challenged on its own. Requesting a single byte of the real
        target is challenged exactly as the download will be, and costs
        nothing to discard.

        The lock matters as much as the probe. Anubis names its outstanding
        challenge in a cookie, so several requests racing for a first challenge
        are issued several that overwrite one another in the shared jar; the
        one that then gets solved no longer matches, and every retry is
        challenged afresh. Letting exactly one request establish the cookie
        removes the race.
        """
        if self.regime is not None:
            return
        async with self._prepare_lock:
            if self.regime is None:
                await self._request("GET", url, headers={"Range": "bytes=0-0"})

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        await self._open.wait()
        issued_at = time.monotonic()
        response = await self._client.request(method, path, **kwargs)
        regime = classify(response)

        if regime is Regime.PROOF_OF_WORK:
            response = await self._pass_challenge(method, path, **kwargs)
            regime = classify(response)
            self.regime = Regime.PROOF_OF_WORK
        else:
            self.regime = regime

        # A scope rejection reaches the client either immediately or only once
        # the challenge is out of the way, so it is judged on the settled
        # response rather than on the first one.
        if regime is Regime.INSUFFICIENT_SCOPE:
            if "Authorization" not in self._client.headers:
                raise AuthRequired(self.host, "the token's scope is too narrow")
            self._drop_token()
            response = await self._client.request(method, path, **kwargs)
            regime = classify(response)
            if regime is Regime.INSUFFICIENT_SCOPE:
                raise AuthRequired(self.host, "the token's scope is too narrow")

        if regime is Regime.AUTH_REQUIRED:
            raise AuthRequired(self.host)

        return response

    async def _pass_challenge(self, method: str, path: str, **kwargs) -> httpx.Response:
        """Clear an Anubis challenge and return the answered response.

        The lock is held across the whole cycle so only one challenge is ever
        outstanding: Anubis tracks the pending challenge in a cookie, and two
        challenges issued concurrently overwrite each other in the shared jar,
        leaving the solved one unmatched. A coroutine that waited retries first,
        because the coroutine ahead of it has usually already made the cookie
        valid and no further proof of work is needed.
        """
        async with self._solve_lock:
            self._open.clear()
            try:
                return await self._answer_challenge(method, path, **kwargs)
            finally:
                self._open.set()

    async def _answer_challenge(self, method: str, path: str, **kwargs) -> httpx.Response:
        response = await self._client.request(method, path, **kwargs)
        if classify(response) is not Regime.PROOF_OF_WORK:
            return response

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

        response = await self._client.request(method, path, **kwargs)
        if classify(response) is Regime.PROOF_OF_WORK:
            raise anubis.ChallengeParseError(
                f"{self.host} still challenges after a solved proof of work"
            )
        return response
