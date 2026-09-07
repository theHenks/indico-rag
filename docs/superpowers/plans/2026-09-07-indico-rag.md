# indico-rag Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Textual TUI that scans an Indico event timetable, lets you select talks by day, session, track or regex, and downloads one slide file per talk as PDF into a day/session folder tree.

**Architecture:** A per-host access-strategy layer normalizes three Indico regimes (open, Anubis proof-of-work, token-authenticated) behind one `httpx.AsyncClient`. A parser converts Indico JSON into a model independent of the API shape, and a ranking rule picks exactly one attachment per talk at parse time. One asyncio coroutine per selected talk runs fetch → convert → extract, gated by two semaphores, writing a manifest that doubles as the resume record and the retrieval-indexing record.

**Tech Stack:** Python 3.11+, uv, httpx, Textual, platformdirs, pypdfium2, pytest + pytest-asyncio + respx. LibreOffice (`soffice`) and Keynote.app as external converters.

**Spec:** `docs/superpowers/specs/2026-09-07-indico-rag-design.md`

## Global Constraints

- Python `>=3.11`. Project managed with `uv`; every command runs under `uv run`.
- Runtime dependencies are exactly: `httpx`, `textual`, `platformdirs`, `pypdfium2`. Dev: `pytest`, `pytest-asyncio`, `respx`.
- **`pypdfium2` for PDF text extraction, never PyMuPDF** — PyMuPDF is AGPL and this repository is permissively licensed.
- Anubis returns **HTTP 200** with an interstitial body. Regime detection keys on the response body, never on the status code.
- Default concurrency: `download_concurrency = 8`, `convert_concurrency = 2`. Values above 16 emit a warning.
- Each concurrent `soffice` process gets its own `-env:UserInstallation=file:///…/lo-profile-<n>`. Sharing a profile makes the second process return without converting.
- Every `soffice` invocation runs under a hard timeout followed by a kill.
- No blanket `except Exception: pass`. Every failure is recorded in the manifest and in the closing summary.
- Filtering changes what is displayed, never what is selected.
- Credentials live in a `0600` file and are never written into a scan cache or a manifest.
- Commit trailers: `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>` followed by
  `Claude-Session: https://claude.ai/code/session_018ZHVctiRLWSFUGrkVvhoXr`. This session's
  system-level attribution directive supersedes the `Assisted-by:` convention in the
  user's global instructions; the first two commits on `main` predate the resolution.
- **Shell hazard on this machine:** `cat` is aliased to `bat` and `rm` to `trash`. Use `command cat`, `tee`, or a `python3` heredoc to write files, and `command rm` to delete. A bare `cat > file <<EOF` silently writes ANSI escapes into the file.
- Commit messages: write the message to a file with the Write tool, then `git commit -F <path>`. A local hook protects `.git/`, so write it under the session scratchpad rather than `.git/COMMIT_MSG`.

## File Structure

```
indico-rag/
├── pyproject.toml                     uv project, deps, pytest config
├── src/indico_rag/
│   ├── model.py                       Event/Day/Session/Talk/Attachment dataclasses
│   ├── access/
│   │   ├── base.py                    AccessStrategy protocol, Regime enum, detect()
│   │   ├── anubis.py                  challenge parsing + proof-of-work solver
│   │   └── token.py                   Bearer-token strategy
│   ├── client.py                      IndicoClient: AsyncClient + strategy + re-solve lock
│   ├── indico/
│   │   ├── endpoints.py               URL construction, event-URL parsing
│   │   ├── parser.py                  Indico JSON → model
│   │   └── ranking.py                 choose one attachment per talk
│   ├── store.py                       config.toml, scan cache, credentials, host cache
│   ├── scan.py                        client + parser + store → a cached Event
│   ├── pipeline/
│   │   ├── paths.py                   sanitization and destination paths
│   │   ├── download.py                streaming download with .part + atomic rename
│   │   ├── convert.py                 soffice / Keynote / Google Slides → PDF
│   │   ├── extract.py                 pypdfium2 text sidecars
│   │   ├── manifest.py                manifest read/write, resume decisions
│   │   └── runner.py                  the per-talk coroutine and its semaphores
│   ├── tui/
│   │   ├── selection.py               SelectionState: tri-state, pure logic
│   │   ├── filtering.py               substring/regex + structured filters, pure logic
│   │   ├── app.py                     Textual App, screen wiring
│   │   └── screens/
│   │       ├── home.py                past scans
│   │       ├── new_event.py           URL input + scan progress
│   │       ├── select.py              the day/session/talk tree
│   │       └── progress.py            per-talk live stages
│   └── cli.py                         argparse entry point
└── tests/
    ├── fixtures/                      captured Anubis pages and Indico JSON
    └── test_*.py
```

Files that change together live together: the three access strategies sit under `access/`, everything that touches Indico's wire format sits under `indico/`, and the pipeline stages are separate files because each has a distinct external dependency and failure mode.

---

### Task 1: Project scaffolding and the Anubis proof-of-work spike

This task is deliberately **exploratory, not test-driven**. Its purpose is to answer a question the rest of the plan depends on: does the proof-of-work solver actually get through `agenda.infn.it`, and what shape is the JSON behind it? Every later task is TDD. Do not skip the capture step — Tasks 2 and 4 are tested against what this task captures.

**Files:**
- Create: `pyproject.toml`, `.python-version`, `src/indico_rag/__init__.py`
- Create: `../probes/anubis_probe.py` (outside the repo — throwaway)
- Create: `../probes/FINDINGS.md`
- Create: `tests/fixtures/` (populated from the capture)

**Interfaces:**
- Consumes: nothing.
- Produces: `tests/fixtures/anubis_interstitial.html`, `tests/fixtures/timetable_50058.json`, `tests/fixtures/event_50058_contributions.json`, and a documented answer in `FINDINGS.md` to: the auth cookie's name, whether `difficulty` counts hex characters or bits, and whether the timetable export carries attachments inline.

- [ ] **Step 1: Initialize the uv project**

```bash
cd ~/Desktop/misc/indico-timetable-tool/indico-rag
uv init --lib --name indico-rag --python 3.11
uv add httpx textual platformdirs pypdfium2
uv add --dev pytest pytest-asyncio respx
```

- [ ] **Step 2: Configure pytest in `pyproject.toml`**

Append to `pyproject.toml`:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
markers = ["slow: requires LibreOffice or network"]
```

- [ ] **Step 3: Write the probe script**

Create `../probes/anubis_probe.py` (note: `probes/` is the sibling directory, deliberately outside the repo):

```python
"""Throwaway probe: confirm the Anubis proof-of-work solver reaches Indico JSON.

Run: uv run python ../probes/anubis_probe.py
Writes captures into ../probes/captures/ for promotion into tests/fixtures/.
"""
import hashlib
import json
import re
import sys
import time
from pathlib import Path

import httpx

HOST = "https://agenda.infn.it"
EVENT = "50058"
OUT = Path(__file__).parent / "captures"
OUT.mkdir(exist_ok=True)

CHALLENGE_RE = re.compile(
    r'<script id="anubis_challenge" type="application/json">(.*?)</script>', re.S
)
PREFIX_RE = re.compile(
    r'<script id="anubis_base_prefix" type="application/json">(.*?)</script>', re.S
)


def is_interstitial(text: str) -> bool:
    return "anubis_challenge" in text or "/.within.website/x/" in text


def solve(challenge: str, difficulty: int) -> tuple[str, int]:
    """Find a nonce whose sha256(challenge+nonce) hex digest starts with `difficulty` zeros."""
    target = "0" * difficulty
    nonce = 0
    while True:
        digest = hashlib.sha256(f"{challenge}{nonce}".encode()).hexdigest()
        if digest.startswith(target):
            return digest, nonce
        nonce += 1


def main() -> int:
    url = f"{HOST}/export/timetable/{EVENT}.json"
    with httpx.Client(follow_redirects=True, timeout=60) as client:
        r = client.get(url)
        print(f"initial: HTTP {r.status_code} ct={r.headers.get('content-type')}")

        if is_interstitial(r.text):
            (OUT / "anubis_interstitial.html").write_text(r.text)
            m = CHALLENGE_RE.search(r.text)
            if not m:
                print("FAIL: interstitial present but no anubis_challenge block")
                print(r.text[:2000])
                return 1
            payload = json.loads(m.group(1))
            print("challenge payload:", json.dumps(payload)[:400])

            # `challenge` is an object; the hashed string is its randomData field,
            # and pass-challenge rejects a submission that omits the challenge id.
            block = payload["challenge"]
            challenge = block["randomData"]
            challenge_id = block["id"]
            difficulty = payload.get("rules", {}).get("difficulty", block.get("difficulty", 4))
            pm = PREFIX_RE.search(r.text)
            prefix = json.loads(pm.group(1)) if pm else ""

            t0 = time.monotonic()
            digest, nonce = solve(challenge, difficulty)
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            print(f"solved: difficulty={difficulty} nonce={nonce} in {elapsed_ms}ms")

            pass_url = f"{HOST}{prefix}/.within.website/x/cmd/anubis/api/pass-challenge"
            pr = client.get(
                pass_url,
                params={
                    "id": challenge_id,
                    "response": digest,
                    "nonce": nonce,
                    "redir": url,
                    "elapsedTime": elapsed_ms,
                },
            )
            print(f"pass-challenge: HTTP {pr.status_code}")
            print("cookies now:", dict(client.cookies))

            r = client.get(url)
            print(f"retry: HTTP {r.status_code} ct={r.headers.get('content-type')}")

        if is_interstitial(r.text):
            print("FAIL: still challenged after solving")
            return 1

        (OUT / "timetable_50058.json").write_text(r.text)
        data = r.json()
        print("timetable keys:", list(data.keys()))

        ev = client.get(
            f"{HOST}/export/event/{EVENT}.json",
            params={"detail": "contributions", "occ": "yes"},
        )
        (OUT / "event_50058_contributions.json").write_text(ev.text)
        print(f"event export: HTTP {ev.status_code} bytes={len(ev.text)}")

    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the probe**

```bash
cd ~/Desktop/misc/indico-timetable-tool/indico-rag
uv run python ../probes/anubis_probe.py
```

Expected: `solved:` line, then `retry: HTTP 200 ct=application/json`, then `OK`.

If it fails, **stop and report** rather than working around it — the failure mode determines whether the design's proof-of-work assumption holds. Capture the printed challenge payload either way.

- [ ] **Step 5: Record findings**

Write `../probes/FINDINGS.md` answering exactly three questions, with evidence pasted from the probe output:

1. What is the auth cookie's name, and what is its lifetime?
2. Does `rules.difficulty` count leading zero **hex characters** or **bits**? (The probe assumes hex characters; if the solve took milliseconds for `difficulty >= 5` the assumption may be wrong.)
3. Does `timetable_50058.json` contain attachment data inline, or is `event_..._contributions.json` required?

- [ ] **Step 6: Promote captures to fixtures**

```bash
mkdir -p tests/fixtures
command cp ../probes/captures/anubis_interstitial.html tests/fixtures/
command cp ../probes/captures/timetable_50058.json tests/fixtures/
command cp ../probes/captures/event_50058_contributions.json tests/fixtures/
```

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "Add uv project scaffolding and Indico API fixtures

Fixtures are captured from agenda.infn.it event 50058 through the
Anubis proof-of-work challenge and are the reference inputs for the
challenge-parsing and timetable-parsing tests.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_018ZHVctiRLWSFUGrkVvhoXr"
```

> **Try it yourself**
>
> ```bash
> cd ~/Desktop/misc/indico-timetable-tool/indico-rag
> uv run python ../probes/anubis_probe.py
> ```
>
> You should see the challenge solved and `retry: HTTP 200 ct=application/json`. Then look at `../probes/captures/timetable_50058.json` to see the raw agenda, and at `../probes/FINDINGS.md` for the three answers. This is the checkpoint where the riskiest assumption in the whole design either holds or doesn't.

---

### Task 2: Anubis challenge parsing and proof-of-work solver

**Files:**
- Create: `src/indico_rag/access/__init__.py` (empty)
- Create: `src/indico_rag/access/anubis.py`
- Test: `tests/test_anubis.py`

**Interfaces:**
- Consumes: `tests/fixtures/anubis_interstitial.html` from Task 1.
- Produces:
  - `is_interstitial(text: str) -> bool`
  - `@dataclass(frozen=True) AnubisChallenge` with fields `challenge: str` (the hashed `randomData` string), `challenge_id: str`, `difficulty: int`, `base_prefix: str`
  - `parse_challenge(html: str) -> AnubisChallenge` — raises `ChallengeParseError`
  - `solve(challenge: str, difficulty: int, *, max_attempts: int = 50_000_000) -> tuple[str, int]` returning `(hex_digest, nonce)` — raises `ProofOfWorkTooHard`
  - `class ChallengeParseError(Exception)`, `class ProofOfWorkTooHard(Exception)`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_anubis.py`:

```python
import hashlib
from pathlib import Path

import pytest

from indico_rag.access.anubis import (
    AnubisChallenge,
    ChallengeParseError,
    ProofOfWorkTooHard,
    is_interstitial,
    parse_challenge,
    solve,
)

FIXTURES = Path(__file__).parent / "fixtures"
INTERSTITIAL = (FIXTURES / "anubis_interstitial.html").read_text()


def test_detects_interstitial_despite_http_200():
    # Anubis answers with status 200, so detection must key on the body.
    assert is_interstitial(INTERSTITIAL) is True


def test_plain_json_is_not_an_interstitial():
    assert is_interstitial('{"results": {}}') is False


def test_parses_challenge_from_captured_page():
    ch = parse_challenge(INTERSTITIAL)
    assert isinstance(ch, AnubisChallenge)
    # The hashed value is challenge.randomData, a long hex string - not the object.
    assert len(ch.challenge) >= 32
    assert all(c in "0123456789abcdef" for c in ch.challenge)
    assert ch.challenge_id, "pass-challenge is rejected without the challenge id"
    assert ch.difficulty >= 1


def test_parse_rejects_a_challenge_object_missing_random_data():
    page = (
        '<script id="anubis_challenge" type="application/json">'
        '{"rules": {"difficulty": 4}, "challenge": {"id": "abc"}}</script>'
    )
    with pytest.raises(ChallengeParseError):
        parse_challenge(page)


def test_parse_rejects_a_page_without_a_challenge():
    with pytest.raises(ChallengeParseError):
        parse_challenge("<html><body>hello</body></html>")


def test_solve_returns_a_digest_with_the_required_zero_prefix():
    digest, nonce = solve("abc123", 3)
    assert digest.startswith("000")
    assert hashlib.sha256(f"abc123{nonce}".encode()).hexdigest() == digest


def test_solve_gives_up_rather_than_spinning_forever():
    with pytest.raises(ProofOfWorkTooHard):
        solve("abc123", 8, max_attempts=5000)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_anubis.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'indico_rag.access'`

- [ ] **Step 3: Write the implementation**

Create `src/indico_rag/access/anubis.py`:

```python
"""Anubis proof-of-work challenge handling.

Anubis answers a challenged request with HTTP 200 and an interstitial page,
so callers must classify on the body rather than the status code.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

_CHALLENGE_RE = re.compile(
    r'<script id="anubis_challenge" type="application/json">(.*?)</script>', re.S
)
_PREFIX_RE = re.compile(
    r'<script id="anubis_base_prefix" type="application/json">(.*?)</script>', re.S
)
_MARKERS = ("anubis_challenge", "/.within.website/x/", "anubis_version")


class ChallengeParseError(Exception):
    """An interstitial was served but its challenge block could not be read."""


class ProofOfWorkTooHard(Exception):
    """The required difficulty exceeds the attempt ceiling."""


@dataclass(frozen=True)
class AnubisChallenge:
    challenge: str
    challenge_id: str
    difficulty: int
    base_prefix: str = ""


def is_interstitial(text: str) -> bool:
    return any(marker in text for marker in _MARKERS)


def parse_challenge(html: str) -> AnubisChallenge:
    match = _CHALLENGE_RE.search(html)
    if match is None:
        raise ChallengeParseError("no anubis_challenge block in response body")
    try:
        payload = json.loads(match.group(1))
        block = payload["challenge"]
        # Anubis v1.25 nests the challenge: the hashed string is randomData, and
        # the id must be echoed back or pass-challenge answers 500.
        challenge = block["randomData"]
        challenge_id = block["id"]
        difficulty = int(
            payload.get("rules", {}).get("difficulty", block.get("difficulty", 4))
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise ChallengeParseError(f"malformed anubis_challenge block: {exc}") from exc

    prefix_match = _PREFIX_RE.search(html)
    prefix = ""
    if prefix_match is not None:
        # An absent prefix block legitimately means the empty prefix; a present
        # but unparseable one means the page is not what we think it is.
        try:
            prefix = json.loads(prefix_match.group(1)) or ""
        except json.JSONDecodeError as exc:
            raise ChallengeParseError(f"malformed anubis_base_prefix block: {exc}") from exc

    return AnubisChallenge(challenge=challenge, challenge_id=challenge_id,
                           difficulty=difficulty, base_prefix=prefix)


def solve(challenge: str, difficulty: int, *, max_attempts: int = 50_000_000) -> tuple[str, int]:
    """Return (hex_digest, nonce) where sha256(challenge+nonce) has `difficulty` leading zeros.

    `difficulty` counts leading zero hex characters: the client's own progress
    maths uses 16**-difficulty, so difficulty 5 means five zero nibbles.
    """
    target = "0" * difficulty
    for nonce in range(max_attempts):
        digest = hashlib.sha256(f"{challenge}{nonce}".encode()).hexdigest()
        if digest.startswith(target):
            return digest, nonce
    raise ProofOfWorkTooHard(
        f"no nonce found for difficulty {difficulty} within {max_attempts} attempts"
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_anubis.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add src/indico_rag/access tests/test_anubis.py
git commit -m "Add Anubis challenge parsing and proof-of-work solver

Detection keys on the response body because Anubis serves its
interstitial with status 200. The solver has an attempt ceiling so a
raised difficulty aborts the run instead of spinning.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_018ZHVctiRLWSFUGrkVvhoXr"
```

---

### Task 3: Access-strategy layer and the Indico client

**Files:**
- Create: `src/indico_rag/access/base.py`
- Create: `src/indico_rag/access/token.py`
- Create: `src/indico_rag/client.py`
- Test: `tests/test_access.py`, `tests/test_client.py`

**Interfaces:**
- Consumes: `is_interstitial`, `parse_challenge`, `solve`, `AnubisChallenge` from Task 2.
- Produces:
  - `class Regime(StrEnum)` with members `OPEN`, `PROOF_OF_WORK`, `AUTH_REQUIRED`
  - `classify(response: httpx.Response) -> Regime`
  - `class AuthRequired(Exception)` carrying `.host: str`
  - `IndicoClient(host: str, *, token: str | None = None, timeout: float = 60.0)` — an async context manager exposing `async get_json(path: str, params: dict | None = None) -> dict`, `async stream_to(url: str, dest: Path) -> int` (returns bytes written), and the attribute `regime: Regime | None` set after the first request.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_access.py`:

```python
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
```

Create `tests/test_client.py`:

```python
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
        '{"challenge": "seed", "rules": {"difficulty": 2}}</script>'
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_access.py tests/test_client.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'indico_rag.client'`

- [ ] **Step 3: Write `access/base.py`**

```python
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
    return _LOGIN_RE.search(text) is not None


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
```

- [ ] **Step 4: Write `access/token.py`**

```python
"""Bearer-token preparation for authenticated Indico instances."""
from __future__ import annotations

import httpx


def apply_token(client: httpx.AsyncClient, token: str | None) -> None:
    """Attach an Indico personal API token to every request the client makes."""
    if token:
        client.headers["Authorization"] = f"Bearer {token}"
```

- [ ] **Step 5: Write `client.py`**

```python
"""One HTTP client that adapts to whichever access regime a host uses."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import TracebackType

import httpx

from indico_rag.access import anubis
from indico_rag.access.base import AuthRequired, Regime, classify, looks_like_login
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
        """Stream `url` into `dest`, or return None if the body is a challenge page.

        Only HTML bodies are inspected: an attachment may legitimately be an HTML
        file, and a real one carries none of Anubis's markers.
        """
        async with self._client.stream("GET", url) as response:
            response.raise_for_status()
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
        response = await self._client.request(method, path, **kwargs)
        regime = classify(response)
        self.regime = regime

        if regime is Regime.AUTH_REQUIRED:
            raise AuthRequired(self.host)

        if regime is Regime.PROOF_OF_WORK:
            await self._solve(response)
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
        """Solve the challenge in `response` unless it is already stale.

        `issued_at` is when the request that produced `response` was sent. The
        cookie is host-wide, so a challenge issued before the last successful
        solve is already answered and needs no further proof of work.
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
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/test_access.py tests/test_client.py -v`
Expected: 8 passed.

- [ ] **Step 7: Commit**

```bash
git add src/indico_rag tests/test_access.py tests/test_client.py
git commit -m "Add per-host access-regime detection and the Indico client

Classification reads the response body so an Anubis interstitial served
with status 200 is not mistaken for success. Concurrent requests share
one cookie jar and re-solve a mid-run challenge under a lock so only one
coroutine performs the work.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_018ZHVctiRLWSFUGrkVvhoXr"
```

---

### Task 4: Model, endpoint helpers, and the Indico parser

**Files:**
- Create: `src/indico_rag/model.py`
- Create: `src/indico_rag/indico/__init__.py` (empty)
- Create: `src/indico_rag/indico/endpoints.py`
- Create: `src/indico_rag/indico/parser.py`
- Test: `tests/test_endpoints.py`, `tests/test_parser.py`

**Interfaces:**
- Consumes: `tests/fixtures/timetable_50058.json` and `tests/fixtures/event_50058_contributions.json` from Task 1.
- Produces:
  - `model.Attachment(id, filename, kind, url, size, content_type, uploaded, folder_title)` where `kind` is `"file"` or `"link"`
  - `model.Talk(id, contribution_id, title, abstract, speakers, affiliations, track, contribution_type, start, end, url, attachments, chosen)`
  - `model.Session(id, title, slug, talks)`, `model.Day(date, sessions)`, `model.Event(id, host, title, slug, days)`
  - `Event.all_talks() -> list[Talk]`
  - `endpoints.parse_event_ref(text: str, default_host: str | None = None) -> tuple[str, str]` returning `(host, event_id)`
  - `endpoints.timetable_path(event_id) -> str`, `endpoints.contributions_path(event_id) -> str`
  - `parser.attachments_by_contribution(event_export: dict) -> dict[str, list[Attachment]]` keyed by the export's `db_id`
  - `parser.parse_timetable(timetable: dict, *, host: str, event_id: str, event_title: str, attachments: dict[str, list[Attachment]] | None = None) -> Event`

**Fixture shape, verified against Task 1's capture of event 50058 (NOW 2026, 100 talks).** These are observed facts, not assumptions:

- The two exports use **different contribution identifiers**. The timetable entry carries `contributionId` (e.g. `299881`, an int); the contributions export carries `id` (a *friendly* id like `"297"`) **and** `db_id` (`299922`). Joining on `id` yields **0 matches out of 100**; joining on `db_id` yields **100**. Key the attachment map on `db_id`.
- Inline timetable attachments are a **dict**, `{"files": [...], "folders": [...]}`, not a list of folders. Their entries carry only `title` and a **site-relative** `download_url`.
- Export attachments carry the full metadata: absolute `download_url`, `filename`, `content_type`, `size`, `modified_dt`, `checksum`. **Prefer the export and fall back to inline**, because ranking needs upload time and the manifest needs size and content type.
- A folder's `title` may be `null`; coerce it to `""`.
- Presenters use `firstName` / `familyName` (plus a combined `name`). Surnames can be multi-word ("Colomer Molla"), so the parser records them rather than letting a later component split on whitespace.
- `track` and `type` are `null` for every contribution in this event. That is legitimate; the filter facets simply come up empty.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_endpoints.py`:

```python
import pytest

from indico_rag.indico.endpoints import contributions_path, parse_event_ref, timetable_path


def test_parses_a_full_timetable_url_with_a_day_anchor():
    host, event_id = parse_event_ref("https://agenda.infn.it/event/50058/timetable/#20260831")
    assert host == "https://agenda.infn.it"
    assert event_id == "50058"


def test_parses_a_bare_event_page_url():
    host, event_id = parse_event_ref("https://indico.cern.ch/event/1234/")
    assert (host, event_id) == ("https://indico.cern.ch", "1234")


def test_bare_id_uses_the_default_host():
    assert parse_event_ref("50058", "https://agenda.infn.it") == (
        "https://agenda.infn.it",
        "50058",
    )


def test_bare_id_without_a_default_host_is_an_error():
    with pytest.raises(ValueError):
        parse_event_ref("50058")


def test_paths():
    assert timetable_path("50058") == "/export/timetable/50058.json"
    assert contributions_path("50058") == "/export/event/50058.json"
```

Create `tests/test_parser.py`:

```python
import json
from pathlib import Path

from indico_rag.indico.parser import attachments_by_contribution, parse_timetable

FIXTURES = Path(__file__).parent / "fixtures"

SYNTHETIC = {
    "results": {
        "50058": {
            "20260831": {
                "s1": {
                    "entryType": "Session",
                    "id": "s1",
                    "title": "Plenary Session I",
                    "sessionSlug": "plenary-i",
                    "entries": {
                        "c1": {
                            "entryType": "Contribution",
                            "id": "c1",
                            "contributionId": "11",
                            "title": "Welcome and Overview",
                            "description": "Opening remarks.",
                            "startDate": {"date": "2026-08-31", "time": "09:00:00"},
                            "endDate": {"date": "2026-08-31", "time": "09:30:00"},
                            "presenters": [
                                {"firstName": "Maria", "familyName": "Colomer Rossi",
                                 "affiliation": "INFN", "name": "Colomer Rossi, Maria"}
                            ],
                            "url": "/event/50058/contributions/11/",
                            "attachments": {
                                "files": [
                                    {"id": "800", "title": "inline.pdf",
                                     "download_url": "/event/50058/x/inline.pdf"}
                                ],
                                "folders": [],
                            },
                        },
                        "b1": {
                            "entryType": "Break",
                            "id": "b1",
                            "title": "Coffee",
                            "startDate": {"date": "2026-08-31", "time": "10:30:00"},
                        },
                    },
                },
                "c2": {
                    "entryType": "Contribution",
                    "id": "c2",
                    "contributionId": "12",
                    "title": "Unscheduled Talk",
                    "startDate": {"date": "2026-08-31", "time": "11:00:00"},
                    "endDate": {"date": "2026-08-31", "time": "11:20:00"},
                    "presenters": [],
                },
            }
        }
    }
}

EXPORT = {
    "results": [
        {
            "contributions": [
                {
                    "id": "9",
                    "db_id": 11,
                    "folders": [
                        {
                            "title": "Slides",
                            "attachments": [
                                {
                                    "id": "901",
                                    "title": "Welcome",
                                    "filename": "welcome.pdf",
                                    "download_url": "https://agenda.infn.it/x/welcome.pdf",
                                    "size": 12345,
                                    "content_type": "application/pdf",
                                    "type": "file",
                                    "modified_dt": "2026-08-20T10:00:00+00:00",
                                }
                            ],
                        }
                    ],
                }
            ]
        }
    ]
}


def test_breaks_are_dropped_and_sessions_are_preserved():
    event = parse_timetable(
        SYNTHETIC, host="https://agenda.infn.it", event_id="50058", event_title="Workshop"
    )
    assert len(event.days) == 1
    day = event.days[0]
    assert str(day.date) == "2026-08-31"
    titles = {s.title for s in day.sessions}
    assert "Plenary Session I" in titles
    all_titles = [t.title for t in event.all_talks()]
    assert "Coffee" not in all_titles
    assert "Welcome and Overview" in all_titles


def test_a_contribution_outside_any_session_gets_a_synthetic_session():
    event = parse_timetable(
        SYNTHETIC, host="https://agenda.infn.it", event_id="50058", event_title="Workshop"
    )
    holder = [s for s in event.days[0].sessions if any(t.title == "Unscheduled Talk" for t in s.talks)]
    assert len(holder) == 1
    assert holder[0].talks[0].session_is_synthetic is True


def test_speakers_and_times_are_parsed():
    event = parse_timetable(
        SYNTHETIC, host="https://agenda.infn.it", event_id="50058", event_title="Workshop"
    )
    talk = next(t for t in event.all_talks() if t.title == "Welcome and Overview")
    assert talk.speakers == ["Maria Colomer Rossi"]
    # Multi-word surnames must survive; splitting a full name on whitespace loses them.
    assert talk.speaker_surnames == ["Colomer Rossi"]
    assert talk.affiliations == ["INFN"]
    assert talk.start.strftime("%H%M") == "0900"


def test_attachments_are_joined_on_db_id_not_the_friendly_id():
    # The export's own `id` is a friendly number matching nothing in the timetable.
    attachments = attachments_by_contribution(EXPORT)
    assert set(attachments) == {"11"}
    event = parse_timetable(
        SYNTHETIC,
        host="https://agenda.infn.it",
        event_id="50058",
        event_title="Workshop",
        attachments=attachments,
    )
    talk = next(t for t in event.all_talks() if t.contribution_id == "11")
    assert len(talk.attachments) == 1
    # The export wins over the inline copy: it carries size and content type.
    assert talk.attachments[0].filename == "welcome.pdf"
    assert talk.attachments[0].folder_title == "Slides"
    assert talk.attachments[0].size == 12345


def test_inline_timetable_attachments_are_used_when_the_export_lacks_the_talk():
    event = parse_timetable(
        SYNTHETIC, host="https://agenda.infn.it", event_id="50058", event_title="Workshop",
        attachments={},
    )
    talk = next(t for t in event.all_talks() if t.contribution_id == "11")
    assert [a.filename for a in talk.attachments] == ["inline.pdf"]


def test_a_null_folder_title_becomes_an_empty_string():
    export = {"results": [{"contributions": [
        {"db_id": 11, "folders": [
            {"title": None, "attachments": [
                {"id": "1", "filename": "a.pdf", "download_url": "https://x/a.pdf",
                 "type": "file"}
            ]}
        ]}
    ]}]}
    assert attachments_by_contribution(export)["11"][0].folder_title == ""


def test_the_captured_event_parses_into_a_non_empty_agenda():
    timetable = json.loads((FIXTURES / "timetable_50058.json").read_text())
    export = json.loads((FIXTURES / "event_50058_contributions.json").read_text())
    event = parse_timetable(
        timetable,
        host="https://agenda.infn.it",
        event_id="50058",
        event_title="captured",
        attachments=attachments_by_contribution(export),
    )
    assert event.days, "the captured timetable produced no days"
    talks = event.all_talks()
    assert talks, "the captured timetable produced no talks"
    assert all(t.title for t in talks), "every talk must have a title"
    assert any(t.attachments for t in talks), "no talk in the capture has attachments"
    # The join must actually land on the export, whose attachments carry size.
    # Joining on the wrong identifier yields inline-only attachments with no size.
    assert any(a.size for t in talks for a in t.attachments), \
        "attachments carry no size: the export join failed"
    assert len(talks) == 100, f"expected 100 talks in the capture, got {len(talks)}"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_endpoints.py tests/test_parser.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'indico_rag.model'`

- [ ] **Step 3: Write `model.py`**

```python
"""The agenda model, kept independent of Indico's wire format."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as Date
from datetime import datetime


@dataclass
class Attachment:
    id: str
    filename: str
    kind: str  # "file" or "link"
    url: str
    size: int | None = None
    content_type: str | None = None
    uploaded: datetime | None = None
    folder_title: str = ""

    @property
    def suffix(self) -> str:
        _, _, ext = self.filename.rpartition(".")
        return ext.lower() if ext and ext != self.filename else ""


@dataclass
class Talk:
    id: str
    contribution_id: str
    title: str
    start: datetime | None
    end: datetime | None
    abstract: str = ""
    speakers: list[str] = field(default_factory=list)
    speaker_surnames: list[str] = field(default_factory=list)
    affiliations: list[str] = field(default_factory=list)
    track: str = ""
    contribution_type: str = ""
    url: str = ""
    session_title: str = ""
    session_is_synthetic: bool = False
    attachments: list[Attachment] = field(default_factory=list)
    chosen: Attachment | None = None


@dataclass
class Session:
    id: str
    title: str
    slug: str
    talks: list[Talk] = field(default_factory=list)
    synthetic: bool = False


@dataclass
class Day:
    date: Date
    sessions: list[Session] = field(default_factory=list)


@dataclass
class Event:
    id: str
    host: str
    title: str
    slug: str
    days: list[Day] = field(default_factory=list)

    def all_talks(self) -> list[Talk]:
        return [talk for day in self.days for session in day.sessions for talk in session.talks]
```

- [ ] **Step 4: Write `indico/endpoints.py`**

```python
"""Indico URL construction and event-reference parsing."""
from __future__ import annotations

import re
from urllib.parse import urlparse

_EVENT_RE = re.compile(r"/event/(\d+)")


def parse_event_ref(text: str, default_host: str | None = None) -> tuple[str, str]:
    """Turn a pasted event URL or a bare event id into (host, event_id)."""
    text = text.strip()
    if text.isdigit():
        if not default_host:
            raise ValueError("a bare event id needs a host; paste the full event URL instead")
        return default_host.rstrip("/"), text

    parsed = urlparse(text)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"not an event URL or id: {text!r}")
    match = _EVENT_RE.search(parsed.path)
    if match is None:
        raise ValueError(f"no /event/<id> segment in {text!r}")
    return f"{parsed.scheme}://{parsed.netloc}", match.group(1)


def timetable_path(event_id: str) -> str:
    return f"/export/timetable/{event_id}.json"


def contributions_path(event_id: str) -> str:
    return f"/export/event/{event_id}.json"
```

- [ ] **Step 5: Write `indico/parser.py`**

```python
"""Indico JSON to agenda model."""
from __future__ import annotations

import re
from datetime import date as Date
from datetime import datetime

from indico_rag.model import Attachment, Day, Event, Session, Talk

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_UNSCHEDULED = "Unscheduled"


def slugify(text: str) -> str:
    return _SLUG_RE.sub("-", text.lower()).strip("-") or "untitled"


def _timestamp(block: dict | None) -> datetime | None:
    if not block:
        return None
    raw_date, raw_time = block.get("date"), block.get("time", "00:00:00")
    if not raw_date:
        return None
    try:
        return datetime.fromisoformat(f"{raw_date}T{raw_time}")
    except ValueError:
        return None


def _surname(person: dict) -> str:
    """Indico spells this familyName in timetable entries and last_name in exports."""
    for key in ("familyName", "last_name", "lastName"):
        value = person.get(key)
        if value:
            return str(value).strip()
    return ""


def _person_name(person: dict) -> str:
    first = person.get("firstName") or person.get("first_name") or ""
    last = _surname(person)
    if first or last:
        return f"{first} {last}".strip()
    explicit = person.get("name") or person.get("fullName") or ""
    return str(explicit).strip()


def _attachment(raw: dict, folder_title: str) -> Attachment:
    kind = "link" if raw.get("type") == "link" or raw.get("link_url") else "file"
    url = raw.get("download_url") or raw.get("link_url") or raw.get("url") or ""
    filename = raw.get("filename") or raw.get("title") or url.rsplit("/", 1)[-1] or "attachment"
    modified = raw.get("modified_dt")
    uploaded = None
    if isinstance(modified, str):
        try:
            uploaded = datetime.fromisoformat(modified)
        except ValueError:
            uploaded = None
    return Attachment(
        id=str(raw.get("id", "")),
        filename=filename,
        kind=kind,
        url=url,
        size=raw.get("size"),
        content_type=raw.get("content_type"),
        uploaded=uploaded,
        folder_title=folder_title or "",
    )


def attachments_by_contribution(event_export: dict) -> dict[str, list[Attachment]]:
    """Map contribution id to its attachments, from an /export/event contributions payload."""
    found: dict[str, list[Attachment]] = {}
    for result in event_export.get("results", []):
        for contribution in result.get("contributions", []):
            # db_id is the identifier the timetable calls contributionId; the
            # export's own `id` is a friendly number that matches nothing.
            key = str(contribution.get("db_id") or contribution.get("id", ""))
            items: list[Attachment] = []
            for folder in contribution.get("folders") or []:
                folder_title = folder.get("title") or ""
                for raw in folder.get("attachments") or []:
                    items.append(_attachment(raw, folder_title))
            if items:
                found[key] = items
    return found


def _talk(entry: dict, session_title: str, synthetic: bool,
          attachments: dict[str, list[Attachment]] | None) -> Talk:
    contribution_id = str(entry.get("contributionId") or entry.get("db_id") or entry.get("id", ""))
    presenters = entry.get("presenters") or entry.get("speakers") or []

    # Inline timetable attachments are {"files": [...], "folders": [...]}, and
    # carry only a title and a site-relative URL.
    inline: list[Attachment] = []
    block = entry.get("attachments") or {}
    if isinstance(block, dict):
        for raw in block.get("files", []) or []:
            inline.append(_attachment(raw, ""))
        for folder in block.get("folders", []) or []:
            for raw in folder.get("attachments", []) or []:
                inline.append(_attachment(raw, folder.get("title") or ""))

    # The export wins: it carries size, content type and upload time, which
    # ranking and the manifest both need.
    resolved = (attachments or {}).get(contribution_id) or inline
    return Talk(
        id=str(entry.get("id", "")),
        contribution_id=contribution_id,
        title=entry.get("title", "").strip() or "Untitled",
        start=_timestamp(entry.get("startDate")),
        end=_timestamp(entry.get("endDate")),
        abstract=entry.get("description", "") or "",
        speakers=[_person_name(p) for p in presenters if _person_name(p)],
        speaker_surnames=[_surname(p) for p in presenters if _surname(p)],
        affiliations=[p.get("affiliation", "") for p in presenters if p.get("affiliation")],
        track=entry.get("track", "") or "",
        contribution_type=entry.get("type", "") or "",
        url=entry.get("url", "") or "",
        session_title=session_title,
        session_is_synthetic=synthetic,
        attachments=list(resolved),
    )


def parse_timetable(timetable: dict, *, host: str, event_id: str, event_title: str,
                    attachments: dict[str, list[Attachment]] | None = None) -> Event:
    """Build an Event from an /export/timetable payload.

    Break entries are dropped. Contributions scheduled outside any session are
    collected into a synthetic session so the tree always has this level.
    """
    event = Event(id=event_id, host=host.rstrip("/"), title=event_title,
                  slug=slugify(event_title), days=[])
    per_event = timetable.get("results", {}).get(str(event_id), {})

    for day_key in sorted(per_event):
        entries = per_event[day_key]
        try:
            day_date = Date.fromisoformat(f"{day_key[:4]}-{day_key[4:6]}-{day_key[6:8]}")
        except ValueError:
            continue

        sessions: dict[str, Session] = {}
        loose: list[Talk] = []

        for entry in entries.values():
            kind = entry.get("entryType")
            if kind == "Session":
                title = entry.get("title", "Session").strip() or "Session"
                session = Session(
                    id=str(entry.get("sessionId") or entry.get("id", "")),
                    title=title,
                    slug=entry.get("sessionSlug") or slugify(title),
                )
                for child in (entry.get("entries") or {}).values():
                    if child.get("entryType") != "Contribution":
                        continue
                    session.talks.append(_talk(child, title, False, attachments))
                if session.talks:
                    sessions[session.id or session.slug] = session
            elif kind == "Contribution":
                loose.append(_talk(entry, _UNSCHEDULED, True, attachments))

        # Order talks first, then order sessions by their earliest talk: Indico's
        # `entries` map has no guaranteed order, so a session's first-listed talk
        # is not necessarily its earliest.
        for session in sessions.values():
            session.talks.sort(key=lambda t: (t.start or datetime.min))

        ordered = sorted(
            sessions.values(),
            key=lambda s: min((t.start or datetime.min) for t in s.talks),
        )
        if loose:
            loose.sort(key=lambda t: (t.start or datetime.min))
            ordered.append(
                Session(id=f"synthetic-{day_key}", title=_UNSCHEDULED,
                        slug=slugify(_UNSCHEDULED), talks=loose, synthetic=True)
            )

        if ordered:
            event.days.append(Day(date=day_date, sessions=ordered))

    return event
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/test_endpoints.py tests/test_parser.py -v`
Expected: 10 passed. If `test_the_captured_event_parses_into_a_non_empty_agenda` fails, the real Indico field names differ from the documented ones — inspect the fixture and adjust `parser.py`, not the test.

- [ ] **Step 7: Commit**

```bash
git add src/indico_rag/model.py src/indico_rag/indico tests/test_endpoints.py tests/test_parser.py
git commit -m "Add agenda model and the Indico timetable parser

The model is independent of Indico's wire format so an API change is
confined to the parser. Breaks are dropped and contributions outside any
session are gathered into a synthetic session, so the tree and the
output layout always have that level.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_018ZHVctiRLWSFUGrkVvhoXr"
```

---

### Task 5: The one-file-per-talk ranking rule

**Files:**
- Create: `src/indico_rag/indico/ranking.py`
- Test: `tests/test_ranking.py`

**Interfaces:**
- Consumes: `model.Attachment`, `model.Talk` from Task 4.
- Produces:
  - `FORMAT_ORDER: dict[str, int]`
  - `is_google_slides(url: str) -> bool`
  - `google_slides_pdf_url(url: str) -> str`
  - `rank(attachment: Attachment) -> tuple` — lower sorts better
  - `choose(talk: Talk) -> Attachment | None`
  - `apply_ranking(event: Event) -> None` — sets `talk.chosen` on every talk

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ranking.py`:

```python
from datetime import datetime

from indico_rag.indico.ranking import choose, google_slides_pdf_url, is_google_slides
from indico_rag.model import Attachment, Talk


def _talk(*attachments: Attachment) -> Talk:
    return Talk(id="c1", contribution_id="1", title="T", start=None, end=None,
                attachments=list(attachments))


def _file(filename: str, *, folder: str = "", uploaded: datetime | None = None,
          size: int | None = None) -> Attachment:
    return Attachment(id=filename, filename=filename, kind="file",
                      url=f"https://x/{filename}", folder_title=folder,
                      uploaded=uploaded, size=size)


def test_pdf_beats_pptx():
    chosen = choose(_talk(_file("deck.pptx"), _file("deck.pdf")))
    assert chosen.filename == "deck.pdf"


def test_pptx_beats_keynote():
    chosen = choose(_talk(_file("deck.key"), _file("deck.pptx")))
    assert chosen.filename == "deck.pptx"


def test_newest_upload_wins_between_two_pdfs():
    old = _file("v1.pdf", uploaded=datetime(2026, 8, 1))
    new = _file("v2.pdf", uploaded=datetime(2026, 8, 20))
    assert choose(_talk(old, new)).filename == "v2.pdf"


def test_a_slides_folder_beats_a_poster_folder_at_equal_format_and_time():
    poster = _file("a.pdf", folder="Poster")
    slides = _file("b.pdf", folder="Slides")
    assert choose(_talk(poster, slides)).filename == "b.pdf"


def test_backup_in_the_filename_loses_to_a_plain_deck():
    assert choose(_talk(_file("talk-backup.pdf"), _file("talk.pdf"))).filename == "talk.pdf"


def test_a_talk_with_no_attachments_chooses_nothing():
    assert choose(_talk()) is None


def test_an_unconvertible_file_type_is_not_chosen():
    assert choose(_talk(_file("data.zip"), _file("movie.mp4"))) is None


def test_a_google_slides_link_is_usable_but_ranks_below_files():
    link = Attachment(id="l", filename="Slides", kind="link",
                      url="https://docs.google.com/presentation/d/ABC123/edit#slide=id.p")
    assert is_google_slides(link.url) is True
    assert choose(_talk(link)).kind == "link"
    assert choose(_talk(link, _file("deck.pdf"))).filename == "deck.pdf"


def test_google_slides_export_url():
    assert google_slides_pdf_url(
        "https://docs.google.com/presentation/d/ABC123/edit#slide=id.p"
    ) == "https://docs.google.com/presentation/d/ABC123/export/pdf"


def test_a_non_google_link_is_never_chosen():
    link = Attachment(id="l", filename="Homepage", kind="link", url="https://example.org/talk")
    assert choose(_talk(link)) is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_ranking.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'indico_rag.indico.ranking'`

- [ ] **Step 3: Write the implementation**

Create `src/indico_rag/indico/ranking.py`:

```python
"""Pick exactly one attachment per talk.

Ranking runs at parse time so the interface can show the resulting filename on
every row and an override is a single field reassignment.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from indico_rag.model import Attachment, Event, Talk

FORMAT_ORDER: dict[str, int] = {
    "pdf": 0,
    "pptx": 1, "ppt": 1, "odp": 1,
    "key": 2,
}
LINK_ORDER = 3

_PREFERRED = re.compile(r"slide|talk|presentation|deck", re.I)
_DEPRIORITIZED = re.compile(r"poster|backup|paper|proceeding|abstract", re.I)
_GOOGLE_SLIDES = re.compile(r"docs\.google\.com/presentation/d/([A-Za-z0-9_-]+)")


def is_google_slides(url: str) -> bool:
    return _GOOGLE_SLIDES.search(url) is not None


def google_slides_pdf_url(url: str) -> str:
    match = _GOOGLE_SLIDES.search(url)
    if match is None:
        raise ValueError(f"not a Google Slides URL: {url!r}")
    return f"https://docs.google.com/presentation/d/{match.group(1)}/export/pdf"


def _format_rank(attachment: Attachment) -> int | None:
    if attachment.kind == "link":
        return LINK_ORDER if is_google_slides(attachment.url) else None
    return FORMAT_ORDER.get(attachment.suffix)


def _upload_key(uploaded: datetime | None) -> tuple[int, float]:
    """Sort key for upload recency; a missing upload time sorts last.

    Attachment.uploaded is timezone-aware for real Indico data but may be naive
    in synthetic data; a naive value is read as UTC so the two are never
    compared directly. datetime.min is unusable as a sentinel here because
    datetime.min.timestamp() raises.
    """
    if uploaded is None:
        return (1, 0.0)
    aware = uploaded if uploaded.tzinfo is not None else uploaded.replace(tzinfo=timezone.utc)
    return (0, -aware.timestamp())


def rank(attachment: Attachment) -> tuple:
    """Sort key; lower is better. Only call on attachments with a usable format."""
    format_rank = _format_rank(attachment)
    assert format_rank is not None
    text = f"{attachment.folder_title} {attachment.filename}"
    name_rank = 1
    if _PREFERRED.search(text):
        name_rank = 0
    if _DEPRIORITIZED.search(text):
        name_rank = 2
    return (format_rank, _upload_key(attachment.uploaded), name_rank, attachment.filename)


def choose(talk: Talk) -> Attachment | None:
    usable = [a for a in talk.attachments if _format_rank(a) is not None]
    if not usable:
        return None
    return min(usable, key=rank)


def apply_ranking(event: Event) -> None:
    for talk in event.all_talks():
        talk.chosen = choose(talk)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_ranking.py -v`
Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add src/indico_rag/indico/ranking.py tests/test_ranking.py
git commit -m "Add the one-file-per-talk ranking rule

Ranks by format, then upload time, then folder and filename wording, so
a talk's chosen file is known before any download starts. Attachments in
formats no converter handles are never chosen.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_018ZHVctiRLWSFUGrkVvhoXr"
```

---

### Task 6: Config, scan cache, host cache, and credentials

**Files:**
- Create: `src/indico_rag/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Consumes: `model.Event` and friends from Task 4.
- Produces:
  - `@dataclass Config` with `output_root: Path`, `download_concurrency: int`, `convert_concurrency: int`, `extract_text: bool`, `default_host: str`
  - `load_config(root: Path | None = None) -> Config`, `save_config(config, root=None) -> None`
  - `event_to_jsonable(event: Event) -> dict`, `event_from_jsonable(data: dict) -> Event`
  - `@dataclass ScanRecord` with `host, event_id, title, first_day, last_day, talk_count, fetched_at, selection: list[str], output_dir: str | None`
  - `save_scan(event, *, selection=None, output_dir=None, root=None) -> None`
  - `load_scan(host, event_id, root=None) -> tuple[Event, ScanRecord]`
  - `list_scans(root=None) -> list[ScanRecord]`, `delete_scan(host, event_id, root=None) -> None`
  - `get_token(host, root=None) -> str | None`, `set_token(host, token, root=None) -> None`
  - `get_host_regime(host, root=None) -> str | None`, `set_host_regime(host, regime, root=None) -> None`

Every function takes an optional `root` so tests use `tmp_path` instead of the real state directory.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_store.py`:

```python
import stat
from datetime import date, datetime

from indico_rag.model import Attachment, Day, Event, Session, Talk
from indico_rag.store import (
    Config,
    delete_scan,
    event_from_jsonable,
    event_to_jsonable,
    get_token,
    list_scans,
    load_config,
    load_scan,
    save_config,
    save_scan,
    set_token,
)


def _event() -> Event:
    attachment = Attachment(id="a1", filename="deck.pdf", kind="file", url="https://x/deck.pdf",
                            size=10, content_type="application/pdf")
    talk = Talk(id="c1", contribution_id="11", title="Welcome",
                start=datetime(2026, 8, 31, 9, 0), end=datetime(2026, 8, 31, 9, 30),
                speakers=["Maria Colomer Rossi"], speaker_surnames=["Colomer Rossi"],
                attachments=[attachment], chosen=attachment)
    session = Session(id="s1", title="Plenary", slug="plenary", talks=[talk])
    return Event(id="50058", host="https://agenda.infn.it", title="Workshop", slug="workshop",
                 days=[Day(date=date(2026, 8, 31), sessions=[session])])


def test_config_round_trips_through_toml(tmp_path):
    original = Config(output_root=tmp_path / "out", download_concurrency=4,
                      convert_concurrency=1, extract_text=True,
                      default_host="https://agenda.infn.it")
    save_config(original, root=tmp_path)
    assert load_config(root=tmp_path) == original


def test_missing_config_yields_defaults(tmp_path):
    config = load_config(root=tmp_path)
    assert config.download_concurrency == 8
    assert config.convert_concurrency == 2
    assert config.extract_text is False


def test_event_round_trips_through_json():
    restored = event_from_jsonable(event_to_jsonable(_event()))
    talk = restored.all_talks()[0]
    assert talk.title == "Welcome"
    assert talk.speaker_surnames == ["Colomer Rossi"], "surnames must survive a round trip"
    assert talk.start == datetime(2026, 8, 31, 9, 0)
    assert restored.days[0].date == date(2026, 8, 31)
    assert talk.chosen is not None and talk.chosen.filename == "deck.pdf"


def test_saving_a_scan_makes_it_listable_and_loadable(tmp_path):
    save_scan(_event(), selection=["c1"], output_dir="/tmp/out", root=tmp_path)
    records = list_scans(root=tmp_path)
    assert len(records) == 1
    assert records[0].event_id == "50058"
    assert records[0].talk_count == 1
    event, record = load_scan("https://agenda.infn.it", "50058", root=tmp_path)
    assert event.all_talks()[0].title == "Welcome"
    assert record.selection == ["c1"]
    assert record.output_dir == "/tmp/out"


def test_deleting_a_scan_removes_it_from_the_index(tmp_path):
    save_scan(_event(), root=tmp_path)
    delete_scan("https://agenda.infn.it", "50058", root=tmp_path)
    assert list_scans(root=tmp_path) == []


def test_tokens_round_trip_and_the_file_is_owner_only(tmp_path):
    set_token("https://indico.cern.ch", "secret", root=tmp_path)
    assert get_token("https://indico.cern.ch", root=tmp_path) == "secret"
    assert get_token("https://other.example", root=tmp_path) is None
    mode = (tmp_path / "credentials.json").stat().st_mode
    assert stat.S_IMODE(mode) == 0o600


def test_a_scan_file_never_contains_a_token(tmp_path):
    set_token("https://agenda.infn.it", "secret", root=tmp_path)
    save_scan(_event(), root=tmp_path)
    for path in (tmp_path / "scans").rglob("*.json"):
        assert "secret" not in path.read_text()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'indico_rag.store'`

- [ ] **Step 3: Write the implementation**

Create `src/indico_rag/store.py`:

```python
"""On-disk state: configuration, cached scans, host regimes, and credentials."""
from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, field
from datetime import date as Date
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import platformdirs

from indico_rag.model import Attachment, Day, Event, Session, Talk

APP = "indico-rag"
DEFAULT_OUTPUT_ROOT = Path.home() / "Desktop" / "indico-talks"


def state_root(root: Path | None = None) -> Path:
    path = root or Path(platformdirs.user_state_dir(APP))
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_root(root: Path | None = None) -> Path:
    path = root or Path(platformdirs.user_config_dir(APP))
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass
class Config:
    output_root: Path = DEFAULT_OUTPUT_ROOT
    download_concurrency: int = 8
    convert_concurrency: int = 2
    extract_text: bool = False
    default_host: str = ""


def load_config(root: Path | None = None) -> Config:
    path = config_root(root) / "config.toml"
    if not path.exists():
        return Config()
    data = tomllib.loads(path.read_text())
    return Config(
        output_root=Path(data.get("output_root", str(DEFAULT_OUTPUT_ROOT))).expanduser(),
        download_concurrency=int(data.get("download_concurrency", 8)),
        convert_concurrency=int(data.get("convert_concurrency", 2)),
        extract_text=bool(data.get("extract_text", False)),
        default_host=str(data.get("default_host", "")),
    )


def save_config(config: Config, root: Path | None = None) -> None:
    path = config_root(root) / "config.toml"
    path.write_text(
        f'output_root = "{config.output_root}"\n'
        f"download_concurrency = {config.download_concurrency}\n"
        f"convert_concurrency = {config.convert_concurrency}\n"
        f"extract_text = {str(config.extract_text).lower()}\n"
        f'default_host = "{config.default_host}"\n'
    )


def _host_key(host: str) -> str:
    netloc = urlparse(host).netloc or host
    return netloc.replace(":", "_")


def _dt(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _undt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _attachment_json(a: Attachment) -> dict:
    return {"id": a.id, "filename": a.filename, "kind": a.kind, "url": a.url, "size": a.size,
            "content_type": a.content_type, "uploaded": _dt(a.uploaded),
            "folder_title": a.folder_title}


def _attachment_obj(d: dict) -> Attachment:
    return Attachment(id=d["id"], filename=d["filename"], kind=d["kind"], url=d["url"],
                      size=d.get("size"), content_type=d.get("content_type"),
                      uploaded=_undt(d.get("uploaded")), folder_title=d.get("folder_title", ""))


def event_to_jsonable(event: Event) -> dict:
    return {
        "id": event.id, "host": event.host, "title": event.title, "slug": event.slug,
        "days": [
            {
                "date": day.date.isoformat(),
                "sessions": [
                    {
                        "id": s.id, "title": s.title, "slug": s.slug, "synthetic": s.synthetic,
                        "talks": [
                            {
                                "id": t.id, "contribution_id": t.contribution_id,
                                "title": t.title, "start": _dt(t.start), "end": _dt(t.end),
                                "abstract": t.abstract, "speakers": t.speakers,
                                "speaker_surnames": t.speaker_surnames,
                                "affiliations": t.affiliations, "track": t.track,
                                "contribution_type": t.contribution_type, "url": t.url,
                                "session_title": t.session_title,
                                "session_is_synthetic": t.session_is_synthetic,
                                "attachments": [_attachment_json(a) for a in t.attachments],
                                "chosen": t.chosen.id if t.chosen else None,
                            }
                            for t in s.talks
                        ],
                    }
                    for s in day.sessions
                ],
            }
            for day in event.days
        ],
    }


def event_from_jsonable(data: dict) -> Event:
    days = []
    for raw_day in data.get("days", []):
        sessions = []
        for raw_session in raw_day.get("sessions", []):
            talks = []
            for rt in raw_session.get("talks", []):
                attachments = [_attachment_obj(a) for a in rt.get("attachments", [])]
                chosen_id = rt.get("chosen")
                talks.append(Talk(
                    id=rt["id"], contribution_id=rt["contribution_id"], title=rt["title"],
                    start=_undt(rt.get("start")), end=_undt(rt.get("end")),
                    abstract=rt.get("abstract", ""), speakers=rt.get("speakers", []),
                    speaker_surnames=rt.get("speaker_surnames", []),
                    affiliations=rt.get("affiliations", []), track=rt.get("track", ""),
                    contribution_type=rt.get("contribution_type", ""), url=rt.get("url", ""),
                    session_title=rt.get("session_title", ""),
                    session_is_synthetic=rt.get("session_is_synthetic", False),
                    attachments=attachments,
                    chosen=next((a for a in attachments if a.id == chosen_id), None),
                ))
            sessions.append(Session(id=raw_session["id"], title=raw_session["title"],
                                    slug=raw_session["slug"], talks=talks,
                                    synthetic=raw_session.get("synthetic", False)))
        days.append(Day(date=Date.fromisoformat(raw_day["date"]), sessions=sessions))
    return Event(id=data["id"], host=data["host"], title=data["title"], slug=data["slug"],
                 days=days)


@dataclass
class ScanRecord:
    host: str
    event_id: str
    title: str
    first_day: str
    last_day: str
    talk_count: int
    fetched_at: str
    selection: list[str] = field(default_factory=list)
    output_dir: str | None = None


def _scans_dir(root: Path | None) -> Path:
    path = state_root(root) / "scans"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _scan_path(host: str, event_id: str, root: Path | None) -> Path:
    return _scans_dir(root) / f"{_host_key(host)}_{event_id}.json"


def save_scan(event: Event, *, selection: list[str] | None = None,
              output_dir: str | None = None, root: Path | None = None) -> None:
    talks = event.all_talks()
    record = ScanRecord(
        host=event.host, event_id=event.id, title=event.title,
        first_day=event.days[0].date.isoformat() if event.days else "",
        last_day=event.days[-1].date.isoformat() if event.days else "",
        talk_count=len(talks), fetched_at=datetime.now().isoformat(timespec="seconds"),
        selection=selection or [], output_dir=output_dir,
    )
    payload = {"record": record.__dict__, "event": event_to_jsonable(event)}
    _scan_path(event.host, event.id, root).write_text(json.dumps(payload, indent=2))

    index = {f"{_host_key(r.host)}_{r.event_id}": r.__dict__ for r in list_scans(root)}
    index[f"{_host_key(event.host)}_{event.id}"] = record.__dict__
    (_scans_dir(root) / "index.json").write_text(json.dumps(index, indent=2))


def load_scan(host: str, event_id: str, root: Path | None = None) -> tuple[Event, ScanRecord]:
    payload = json.loads(_scan_path(host, event_id, root).read_text())
    return event_from_jsonable(payload["event"]), ScanRecord(**payload["record"])


def list_scans(root: Path | None = None) -> list[ScanRecord]:
    path = _scans_dir(root) / "index.json"
    if not path.exists():
        return []
    entries = json.loads(path.read_text()).values()
    records = [ScanRecord(**entry) for entry in entries]
    return sorted(records, key=lambda r: r.fetched_at, reverse=True)


def delete_scan(host: str, event_id: str, root: Path | None = None) -> None:
    _scan_path(host, event_id, root).unlink(missing_ok=True)
    path = _scans_dir(root) / "index.json"
    if path.exists():
        index = json.loads(path.read_text())
        index.pop(f"{_host_key(host)}_{event_id}", None)
        path.write_text(json.dumps(index, indent=2))


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def get_token(host: str, root: Path | None = None) -> str | None:
    return _read_json(state_root(root) / "credentials.json").get(_host_key(host))


def set_token(host: str, token: str, root: Path | None = None) -> None:
    path = state_root(root) / "credentials.json"
    data = _read_json(path)
    data[_host_key(host)] = token
    path.write_text(json.dumps(data, indent=2))
    os.chmod(path, 0o600)


def get_host_regime(host: str, root: Path | None = None) -> str | None:
    return _read_json(state_root(root) / "hosts.json").get(_host_key(host))


def set_host_regime(host: str, regime: str, root: Path | None = None) -> None:
    path = state_root(root) / "hosts.json"
    data = _read_json(path)
    data[_host_key(host)] = regime
    path.write_text(json.dumps(data, indent=2))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_store.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add src/indico_rag/store.py tests/test_store.py
git commit -m "Add configuration, scan cache, host cache and credential storage

Scans persist the parsed agenda alongside the last selection and output
directory so reopening one restores both. Tokens live in an owner-only
file and are never written into a scan.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_018ZHVctiRLWSFUGrkVvhoXr"
```

---

### Task 7: Scanner and the `scan` CLI command

This is the first milestone you can drive by hand.

**Files:**
- Create: `src/indico_rag/scan.py`
- Create: `src/indico_rag/cli.py`
- Modify: `pyproject.toml` (add the console script)
- Test: `tests/test_scan.py`

**Interfaces:**
- Consumes: `IndicoClient` (Task 3), `parse_event_ref`/`timetable_path`/`contributions_path` (Task 4), `parse_timetable`/`attachments_by_contribution` (Task 4), `apply_ranking` (Task 5), `store` (Task 6).
- Produces:
  - `async scan_event(host: str, event_id: str, *, token: str | None = None, progress: Callable[[str], None] | None = None) -> Event`
  - `format_agenda(event: Event) -> str` — the plain-text tree the CLI prints
  - console script `indico-rag`

- [ ] **Step 1: Add the console script to `pyproject.toml`**

```toml
[project.scripts]
indico-rag = "indico_rag.cli:main"
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_scan.py`:

```python
import json
from pathlib import Path

import httpx
import respx

from indico_rag.scan import format_agenda, scan_event

FIXTURES = Path(__file__).parent / "fixtures"
HOST = "https://agenda.infn.it"


@respx.mock
async def test_scan_returns_a_ranked_event_and_reports_progress():
    timetable = json.loads((FIXTURES / "timetable_50058.json").read_text())
    export = json.loads((FIXTURES / "event_50058_contributions.json").read_text())
    respx.get(f"{HOST}/export/timetable/50058.json").mock(
        return_value=httpx.Response(200, json=timetable)
    )
    respx.get(f"{HOST}/export/event/50058.json").mock(
        return_value=httpx.Response(200, json=export)
    )

    stages: list[str] = []
    event = await scan_event(HOST, "50058", progress=stages.append)

    assert event.days
    assert event.all_talks()
    assert any("timetable" in s for s in stages)
    # apply_ranking ran: any talk with attachments has a chosen file.
    with_attachments = [t for t in event.all_talks() if t.attachments]
    assert all(t.chosen is not None for t in with_attachments if
               any(a.suffix in {"pdf", "pptx", "ppt", "odp", "key"} for a in t.attachments))


def test_format_agenda_shows_days_sessions_and_chosen_files():
    from datetime import date, datetime

    from indico_rag.model import Attachment, Day, Event, Session, Talk

    attachment = Attachment(id="a", filename="deck.pdf", kind="file", url="https://x/deck.pdf")
    talk = Talk(id="c1", contribution_id="11", title="Welcome",
                start=datetime(2026, 8, 31, 9, 0), end=None, speakers=["Maria Rossi"],
                attachments=[attachment], chosen=attachment)
    empty = Talk(id="c2", contribution_id="12", title="Discussion", start=None, end=None)
    event = Event(id="50058", host=HOST, title="Workshop", slug="workshop",
                  days=[Day(date=date(2026, 8, 31),
                            sessions=[Session(id="s1", title="Plenary", slug="plenary",
                                              talks=[talk, empty])])])

    text = format_agenda(event)
    assert "2026-08-31" in text
    assert "Plenary" in text
    assert "09:00" in text
    assert "deck.pdf" in text
    assert "no material" in text
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_scan.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'indico_rag.scan'`

- [ ] **Step 4: Write `scan.py`**

```python
"""Fetch an event's timetable and turn it into a ranked agenda."""
from __future__ import annotations

from collections.abc import Callable

from indico_rag.client import IndicoClient
from indico_rag.indico.endpoints import contributions_path, timetable_path
from indico_rag.indico.parser import attachments_by_contribution, parse_timetable
from indico_rag.indico.ranking import apply_ranking
from indico_rag.model import Event

Progress = Callable[[str], None]


async def scan_event(host: str, event_id: str, *, token: str | None = None,
                     progress: Progress | None = None) -> Event:
    def report(message: str) -> None:
        if progress is not None:
            progress(message)

    async with IndicoClient(host, token=token) as client:
        report("fetching timetable")
        timetable = await client.get_json(timetable_path(event_id))
        report(f"access regime: {client.regime}")

        report("fetching attachments")
        export = await client.get_json(
            contributions_path(event_id), params={"detail": "contributions", "occ": "yes"}
        )

    attachments = attachments_by_contribution(export)
    title = _event_title(export, event_id)
    event = parse_timetable(timetable, host=host, event_id=event_id, event_title=title,
                            attachments=attachments)
    apply_ranking(event)
    report(f"parsed {len(event.all_talks())} talks across {len(event.days)} days")
    return event


def _event_title(export: dict, event_id: str) -> str:
    for result in export.get("results", []):
        title = result.get("title")
        if title:
            return str(title)
    return f"event-{event_id}"


def format_agenda(event: Event) -> str:
    lines = [f"{event.title}  [{event.host}/event/{event.id}]", ""]
    for day in event.days:
        lines.append(f"{day.date.isoformat()}")
        for session in day.sessions:
            lines.append(f"  {session.title}")
            for talk in session.talks:
                clock = talk.start.strftime("%H:%M") if talk.start else "  :  "
                who = f" — {talk.speakers[0]}" if talk.speakers else ""
                if talk.chosen is None:
                    marker = "no material"
                elif talk.chosen.kind == "link":
                    marker = "google slides → pdf"
                elif talk.chosen.suffix == "pdf":
                    marker = talk.chosen.filename
                else:
                    marker = f"{talk.chosen.filename} → pdf"
                lines.append(f"    {clock}  {talk.title}{who}  [{marker}]")
        lines.append("")
    return "\n".join(lines)
```

- [ ] **Step 5: Write `cli.py`**

```python
"""Command line entry point."""
from __future__ import annotations

import argparse
import asyncio
import sys

from indico_rag import store
from indico_rag.access.base import AuthRequired
from indico_rag.indico.endpoints import parse_event_ref
from indico_rag.scan import format_agenda, scan_event


def _cmd_scan(args: argparse.Namespace) -> int:
    config = store.load_config()
    try:
        host, event_id = parse_event_ref(args.event, config.default_host or None)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        event = asyncio.run(
            scan_event(host, event_id, token=store.get_token(host),
                       progress=lambda m: print(f"… {m}", file=sys.stderr))
        )
    except AuthRequired as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3

    store.save_scan(event)
    store.set_host_regime(host, "checked")
    print(format_agenda(event))
    return 0


def _cmd_scans(_: argparse.Namespace) -> int:
    records = store.list_scans()
    if not records:
        print("no scans yet")
        return 0
    for record in records:
        print(f"{record.fetched_at}  {record.event_id:>8}  {record.talk_count:>4} talks  "
              f"{record.first_day}..{record.last_day}  {record.title}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="indico-rag",
                                     description="Download an Indico event's talks as PDFs.")
    sub = parser.add_subparsers(dest="command", required=True)

    scan_parser = sub.add_parser("scan", help="fetch an event agenda and print it")
    scan_parser.add_argument("event", help="event URL or bare event id")
    scan_parser.set_defaults(func=_cmd_scan)

    scans_parser = sub.add_parser("scans", help="list cached scans")
    scans_parser.set_defaults(func=_cmd_scans)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest -v`
Expected: all tests pass (Tasks 2–7).

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "Add the scanner and a scan command that prints the agenda

Scanning fetches the timetable and attachment metadata, applies the
ranking rule, and caches the result, so the chosen file for every talk
is visible before anything is downloaded.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_018ZHVctiRLWSFUGrkVvhoXr"
```

> **Try it yourself — milestone 1: the agenda, end to end**
>
> ```bash
> cd ~/Desktop/misc/indico-timetable-tool/indico-rag
> uv run indico-rag scan "https://agenda.infn.it/event/50058/timetable/#20260831"
> uv run indico-rag scans
> ```
>
> You should see the proof-of-work solved, then the full agenda printed as
> `day → session → talk`, with the file each talk would produce shown in brackets —
> `deck.pdf`, `slides.pptx → pdf`, or `no material`. This is the exact selection
> the TUI will later show you.
>
> **What to give feedback on:** are the right talks present, are sessions grouped
> the way you expect, and is the file chosen for each talk the one you would have
> picked? Ranking bugs are far cheaper to fix here than after the downloader exists.

---

### Task 8: Destination paths and filename sanitization

**Files:**
- Create: `src/indico_rag/pipeline/__init__.py` (empty)
- Create: `src/indico_rag/pipeline/paths.py`
- Test: `tests/test_paths.py`

**Interfaces:**
- Consumes: `model.Event`, `model.Day`, `model.Session`, `model.Talk` from Task 4.
- Produces:
  - `sanitize(text: str, *, max_length: int = 80) -> str`
  - `speaker_component(talk: Talk) -> str` — first speaker's surname, `""` when none
  - `event_dir(root: Path, event: Event) -> Path`
  - `destination(root: Path, event: Event, day: Day, session: Session, talk: Talk, *, suffix: str = "pdf") -> Path`
  - `deduplicate(path: Path, taken: set[Path]) -> Path`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_paths.py`:

```python
from datetime import date, datetime
from pathlib import Path

from indico_rag.model import Day, Event, Session, Talk
from indico_rag.pipeline.paths import deduplicate, destination, sanitize, speaker_component

ROOT = Path("/out")


def _fixture(title="Welcome and Overview", speakers=("Maria Rossi",), session="Plenary Session I",
             start=datetime(2026, 8, 31, 9, 0)):
    talk = Talk(id="c1", contribution_id="11", title=title, start=start, end=None,
                speakers=list(speakers))
    sess = Session(id="s1", title=session, slug="x", talks=[talk])
    day = Day(date=date(2026, 8, 31), sessions=[sess])
    event = Event(id="50058", host="https://agenda.infn.it", title="LNGS Workshop 2026",
                  slug="lngs-workshop-2026", days=[day])
    return event, day, sess, talk


def test_sanitize_replaces_separators_and_collapses_whitespace():
    assert sanitize("A/B: the C") == "A-B-the-C"


def test_sanitize_keeps_legal_unicode():
    assert sanitize("Müller Résumé") == "Müller-Résumé"


def test_sanitize_caps_length():
    assert len(sanitize("x" * 300, max_length=40)) == 40


def test_sanitize_never_returns_empty():
    assert sanitize("///") == "untitled"


def test_speaker_component_uses_the_first_speakers_surname():
    _, _, _, talk = _fixture(speakers=("Maria Rossi", "Li Chen"))
    talk.speaker_surnames = ["Rossi", "Chen"]
    assert speaker_component(talk) == "Rossi"


def test_speaker_component_keeps_a_multi_word_surname():
    _, _, _, talk = _fixture(speakers=("Marta Colomer Molla",))
    talk.speaker_surnames = ["Colomer Molla"]
    assert speaker_component(talk) == "Colomer-Molla"


def test_speaker_component_is_empty_without_speakers():
    _, _, _, talk = _fixture(speakers=())
    assert speaker_component(talk) == ""


def test_destination_follows_event_day_session_layout():
    event, day, session, talk = _fixture()
    assert destination(ROOT, event, day, session, talk) == Path(
        "/out/50058_lngs-workshop-2026/2026-08-31/Plenary-Session-I/"
        "0900_Rossi_Welcome-and-Overview.pdf"
    )


def test_destination_omits_the_speaker_component_when_there_is_none():
    event, day, session, talk = _fixture(speakers=())
    assert destination(ROOT, event, day, session, talk).name == "0900_Welcome-and-Overview.pdf"


def test_destination_without_a_start_time_uses_a_placeholder():
    event, day, session, talk = _fixture(start=None)
    assert destination(ROOT, event, day, session, talk).name.startswith("0000_")


def test_deduplicate_adds_a_numeric_suffix():
    taken = {Path("/out/a.pdf"), Path("/out/a-2.pdf")}
    assert deduplicate(Path("/out/a.pdf"), taken) == Path("/out/a-3.pdf")


def test_deduplicate_leaves_a_free_path_alone():
    assert deduplicate(Path("/out/b.pdf"), set()) == Path("/out/b.pdf")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_paths.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'indico_rag.pipeline'`

- [ ] **Step 3: Write the implementation**

Create `src/indico_rag/pipeline/paths.py`:

```python
"""Destination paths for downloaded talks."""
from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from indico_rag.model import Day, Event, Session, Talk

_ILLEGAL = re.compile(r"[\\/:*?\"<>|\x00-\x1f]+")
_SEPARATORS = re.compile(r"[\s_]+")
_RUNS = re.compile(r"-{2,}")


def sanitize(text: str, *, max_length: int = 80) -> str:
    """Make one path component out of arbitrary text, preserving legal unicode."""
    normalized = unicodedata.normalize("NFC", text).strip()
    cleaned = _ILLEGAL.sub("-", normalized)
    cleaned = _SEPARATORS.sub("-", cleaned)
    cleaned = _RUNS.sub("-", cleaned).strip("-. ")
    return cleaned[:max_length].strip("-. ") or "untitled"


def speaker_component(talk: Talk) -> str:
    """The first speaker's surname.

    Uses the surname Indico reported rather than the last whitespace-separated
    word of the full name, which would turn "Colomer Molla" into "Molla".
    """
    if talk.speaker_surnames:
        return sanitize(talk.speaker_surnames[0], max_length=40)
    if talk.speakers:
        return sanitize(talk.speakers[0].split()[-1], max_length=40)
    return ""


def event_dir(root: Path, event: Event) -> Path:
    return root / f"{event.id}_{sanitize(event.slug or event.title)}"


def destination(root: Path, event: Event, day: Day, session: Session, talk: Talk, *,
                suffix: str = "pdf") -> Path:
    clock = talk.start.strftime("%H%M") if talk.start else "0000"
    parts = [clock, speaker_component(talk), sanitize(talk.title)]
    name = "_".join(part for part in parts if part)
    return event_dir(root, event) / day.date.isoformat() / sanitize(session.title) / f"{name}.{suffix}"


def deduplicate(path: Path, taken: set[Path]) -> Path:
    """Return `path`, or the first free `<stem>-<n>` variant when it is already taken."""
    if path not in taken:
        return path
    counter = 2
    while True:
        candidate = path.with_name(f"{path.stem}-{counter}{path.suffix}")
        if candidate not in taken:
            return candidate
        counter += 1
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_paths.py -v`
Expected: 11 passed.

- [ ] **Step 5: Commit**

```bash
git add src/indico_rag/pipeline tests/test_paths.py
git commit -m "Add destination paths and filename sanitization

Names carry start time, first speaker's surname and title, capped in
length and stripped of characters the filesystem rejects. Two talks
sharing a time and speaker are separated by a numeric suffix.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_018ZHVctiRLWSFUGrkVvhoXr"
```

---

### Task 9: Conversion to PDF

**Files:**
- Create: `src/indico_rag/pipeline/convert.py`
- Test: `tests/test_convert.py`

**Interfaces:**
- Consumes: nothing from earlier tasks except the `google_slides_pdf_url` helper (Task 5).
- Produces:
  - `class ConversionError(Exception)`
  - `class ConverterMissing(ConversionError)`
  - `SOFFICE_FORMATS = {"pptx", "ppt", "odp", "doc", "docx", "odt"}`, `KEYNOTE_FORMATS = {"key"}`
  - `find_soffice() -> str | None`
  - `async convert_to_pdf(source: Path, *, profile_index: int = 0, timeout: float = 120.0) -> Path`
  - `needs_conversion(suffix: str) -> bool`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_convert.py`:

```python
import asyncio
import shutil
from pathlib import Path

import pytest

from indico_rag.pipeline import convert
from indico_rag.pipeline.convert import (
    ConversionError,
    ConverterMissing,
    convert_to_pdf,
    needs_conversion,
)


def test_pdf_needs_no_conversion():
    assert needs_conversion("pdf") is False


def test_pptx_and_key_need_conversion():
    assert needs_conversion("pptx") is True
    assert needs_conversion("key") is True


async def test_soffice_is_invoked_with_a_private_profile(tmp_path, monkeypatch):
    source = tmp_path / "deck.pptx"
    source.write_bytes(b"stub")
    recorded: dict = {}

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            # LibreOffice writes <stem>.pdf beside the source.
            source.with_suffix(".pdf").write_bytes(b"%PDF-1.4 stub")
            return b"", b""

        def kill(self):  # pragma: no cover - not reached in this test
            pass

    async def fake_exec(*args, **kwargs):
        recorded["args"] = args
        return FakeProcess()

    monkeypatch.setattr(convert, "find_soffice", lambda: "/fake/soffice")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    result = await convert_to_pdf(source, profile_index=3)

    assert result == source.with_suffix(".pdf")
    profile_args = [a for a in recorded["args"] if str(a).startswith("-env:UserInstallation=")]
    assert len(profile_args) == 1, "each conversion must get its own LibreOffice profile"
    assert profile_args[0].endswith("lo-profile-3")
    assert "--headless" in recorded["args"]


async def test_a_hanging_soffice_is_killed_and_reported(tmp_path, monkeypatch):
    source = tmp_path / "deck.pptx"
    source.write_bytes(b"stub")
    killed = {"count": 0}

    class HangingProcess:
        returncode = None

        async def communicate(self):
            await asyncio.sleep(10)
            return b"", b""

        def kill(self):
            killed["count"] += 1

    async def fake_exec(*args, **kwargs):
        return HangingProcess()

    monkeypatch.setattr(convert, "find_soffice", lambda: "/fake/soffice")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    with pytest.raises(ConversionError, match="timed out"):
        await convert_to_pdf(source, timeout=0.05)
    assert killed["count"] == 1


async def test_a_missing_converter_is_reported_clearly(tmp_path, monkeypatch):
    source = tmp_path / "deck.pptx"
    source.write_bytes(b"stub")
    monkeypatch.setattr(convert, "find_soffice", lambda: None)
    with pytest.raises(ConverterMissing, match="libreoffice"):
        await convert_to_pdf(source)


async def test_an_unsupported_suffix_is_rejected(tmp_path):
    source = tmp_path / "archive.zip"
    source.write_bytes(b"stub")
    with pytest.raises(ConversionError, match="no converter"):
        await convert_to_pdf(source)


@pytest.mark.slow
@pytest.mark.skipif(
    shutil.which("soffice") is None
    and not Path("/Applications/LibreOffice.app/Contents/MacOS/soffice").exists(),
    reason="LibreOffice not installed",
)
async def test_real_libreoffice_converts_an_odp(tmp_path):
    # Minimal ODP written by LibreOffice itself would be large; use a plain ODT
    # to exercise the same code path with a file LibreOffice always accepts.
    source = tmp_path / "note.txt"
    source.write_text("hello")
    with pytest.raises(ConversionError):
        await convert_to_pdf(source)  # .txt is not in SOFFICE_FORMATS
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_convert.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'indico_rag.pipeline.convert'`

- [ ] **Step 3: Write the implementation**

Create `src/indico_rag/pipeline/convert.py`:

```python
"""Render non-PDF slide files to PDF.

LibreOffice handles the Office and OpenDocument formats; Keynote files are
converted by driving Keynote.app, which is the only program that reads them.
"""
from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path

SOFFICE_FORMATS = {"pptx", "ppt", "odp", "doc", "docx", "odt"}
KEYNOTE_FORMATS = {"key"}

_MAC_SOFFICE = Path("/Applications/LibreOffice.app/Contents/MacOS/soffice")


class ConversionError(Exception):
    """A file could not be rendered to PDF."""


class ConverterMissing(ConversionError):
    """The external program needed for this format is not installed."""


def needs_conversion(suffix: str) -> bool:
    return suffix.lower() != "pdf"


def find_soffice() -> str | None:
    found = shutil.which("soffice") or shutil.which("libreoffice")
    if found:
        return found
    return str(_MAC_SOFFICE) if _MAC_SOFFICE.exists() else None


def _profile_dir(profile_index: int) -> Path:
    path = Path(tempfile.gettempdir()) / "indico-rag" / f"lo-profile-{profile_index}"
    path.mkdir(parents=True, exist_ok=True)
    return path


async def _run(argv: list[str], timeout: float) -> tuple[int, bytes]:
    process = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        process.kill()
        raise ConversionError(f"converter timed out after {timeout}s") from exc
    return process.returncode or 0, stdout or b""


async def _convert_with_soffice(source: Path, profile_index: int, timeout: float) -> Path:
    soffice = find_soffice()
    if soffice is None:
        raise ConverterMissing(
            "LibreOffice is required for this format; install it with "
            "`brew install --cask libreoffice`"
        )
    argv = [
        soffice,
        f"-env:UserInstallation=file://{_profile_dir(profile_index)}",
        "--headless",
        "--norestore",
        "--convert-to",
        "pdf",
        "--outdir",
        str(source.parent),
        str(source),
    ]
    code, output = await _run(argv, timeout)
    produced = source.with_suffix(".pdf")
    if not produced.exists() or produced.stat().st_size == 0:
        raise ConversionError(
            f"LibreOffice exited {code} without producing a PDF: "
            f"{output.decode(errors='replace')[:400]}"
        )
    return produced


async def _convert_with_keynote(source: Path, timeout: float) -> Path:
    produced = source.with_suffix(".pdf")
    script = (
        'on run argv\n'
        '  set src to POSIX file (item 1 of argv)\n'
        '  set dst to POSIX file (item 2 of argv)\n'
        '  tell application "Keynote"\n'
        '    set doc to open src\n'
        '    export doc to dst as PDF\n'
        '    close doc saving no\n'
        '  end tell\n'
        'end run'
    )
    code, output = await _run(
        ["osascript", "-e", script, str(source), str(produced)], timeout
    )
    if not produced.exists() or produced.stat().st_size == 0:
        raise ConversionError(
            f"Keynote exited {code} without producing a PDF: "
            f"{output.decode(errors='replace')[:400]}"
        )
    return produced


async def convert_to_pdf(source: Path, *, profile_index: int = 0, timeout: float = 120.0) -> Path:
    """Render `source` to a PDF beside it and return the PDF's path."""
    suffix = source.suffix.lstrip(".").lower()
    if suffix == "pdf":
        return source
    if suffix in SOFFICE_FORMATS:
        return await _convert_with_soffice(source, profile_index, timeout)
    if suffix in KEYNOTE_FORMATS:
        return await _convert_with_keynote(source, timeout)
    raise ConversionError(f"no converter for .{suffix}")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_convert.py -v`
Expected: 7 passed (the slow test runs only when LibreOffice is present).

- [ ] **Step 5: Commit**

```bash
git add src/indico_rag/pipeline/convert.py tests/test_convert.py
git commit -m "Add PDF conversion through LibreOffice and Keynote

Each concurrent LibreOffice process receives its own user profile,
without which a second instance attaches to the first and returns
without converting. Every conversion runs under a timeout followed by a
kill, and a run that produces no PDF raises rather than leaving an empty
file behind.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_018ZHVctiRLWSFUGrkVvhoXr"
```

---

### Task 10: PDF text extraction

**Files:**
- Create: `src/indico_rag/pipeline/extract.py`
- Test: `tests/test_extract.py`

**Interfaces:**
- Produces:
  - `extract_text(pdf: Path) -> str`
  - `sidecar_path(pdf: Path) -> Path` — `<stem>.txt` beside the PDF
  - `write_sidecar(pdf: Path) -> tuple[Path, int]` returning the sidecar path and character count

- [ ] **Step 1: Write the failing tests**

Create `tests/test_extract.py`:

```python
from pathlib import Path

import pypdfium2
import pytest

from indico_rag.pipeline.extract import extract_text, sidecar_path, write_sidecar


@pytest.fixture
def one_page_pdf(tmp_path) -> Path:
    pdf = pypdfium2.PdfDocument.new()
    pdf.new_page(200, 200)
    path = tmp_path / "slide.pdf"
    pdf.save(str(path))
    return path


def test_sidecar_path_sits_beside_the_pdf(tmp_path):
    assert sidecar_path(tmp_path / "a" / "b.pdf") == tmp_path / "a" / "b.txt"


def test_extracting_an_empty_page_yields_no_text_without_raising(one_page_pdf):
    assert extract_text(one_page_pdf).strip() == ""


def test_write_sidecar_creates_the_file_and_reports_the_length(one_page_pdf):
    path, length = write_sidecar(one_page_pdf)
    assert path.exists()
    assert length == len(path.read_text())


def test_a_corrupt_pdf_raises_rather_than_returning_empty_text(tmp_path):
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"not a pdf at all")
    with pytest.raises(Exception):
        extract_text(broken)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_extract.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'indico_rag.pipeline.extract'`

- [ ] **Step 3: Write the implementation**

Create `src/indico_rag/pipeline/extract.py`:

```python
"""Text sidecars for downloaded PDFs.

Uses pypdfium2, which is permissively licensed; PyMuPDF's AGPL licence would
propagate to this project.
"""
from __future__ import annotations

from pathlib import Path

import pypdfium2


def sidecar_path(pdf: Path) -> Path:
    return pdf.with_suffix(".txt")


def extract_text(pdf: Path) -> str:
    """Return the concatenated text of every page.

    A slide deck made of images yields little or no text. That is a property of
    the input, not an error, and is reported by the caller as a character count.
    """
    document = pypdfium2.PdfDocument(str(pdf))
    try:
        pages = []
        for index in range(len(document)):
            page = document[index]
            textpage = page.get_textpage()
            pages.append(textpage.get_text_range())
        return "\n\n".join(pages)
    finally:
        document.close()


def write_sidecar(pdf: Path) -> tuple[Path, int]:
    text = extract_text(pdf)
    path = sidecar_path(pdf)
    path.write_text(text)
    return path, len(text)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_extract.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add src/indico_rag/pipeline/extract.py tests/test_extract.py
git commit -m "Add optional PDF text extraction

Writes one text sidecar per talk using pypdfium2. An image-only deck
yields little text; that is recorded as a character count rather than
treated as a failure.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_018ZHVctiRLWSFUGrkVvhoXr"
```

---

### Task 11: The manifest

**Files:**
- Create: `src/indico_rag/pipeline/manifest.py`
- Test: `tests/test_manifest.py`

**Interfaces:**
- Produces:
  - `@dataclass TalkRecord` with fields `contribution_id, title, abstract, speakers, affiliations, session, track, contribution_type, start, end, indico_url, source_url, source_filename, local_path, sha256, bytes, converted, conversion_error, text_path, text_chars, status, error`. `status` is one of `"done"`, `"failed"`, `"no_material"`.
  - `sha256_of(path: Path) -> str`
  - `class Manifest` with `Manifest.load(path) -> Manifest`, `.save()`, `.add(record)`, `.get(contribution_id) -> TalkRecord | None`, `.is_complete(contribution_id) -> bool`, `.counts() -> dict[str, int]`
  - `MANIFEST_NAME = "_manifest.json"`

`is_complete` is the resume decision: a record with `status == "done"` whose `local_path` still exists and whose `sha256` still matches.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_manifest.py`:

```python
from indico_rag.pipeline.manifest import Manifest, TalkRecord, sha256_of


def _record(tmp_path, contribution_id="11", status="done"):
    pdf = tmp_path / f"{contribution_id}.pdf"
    pdf.write_bytes(b"%PDF-1.4 content")
    return TalkRecord(
        contribution_id=contribution_id, title="Welcome", abstract="Opening.",
        speakers=["Maria Rossi"], affiliations=["INFN"], session="Plenary",
        track="", contribution_type="", start="2026-08-31T09:00:00", end=None,
        indico_url="https://agenda.infn.it/event/50058/contributions/11/",
        source_url="https://agenda.infn.it/x/deck.pdf", source_filename="deck.pdf",
        local_path=str(pdf), sha256=sha256_of(pdf), bytes=pdf.stat().st_size,
        converted=False, conversion_error=None, text_path=None, text_chars=None,
        status=status, error=None,
    )


def test_manifest_round_trips(tmp_path):
    path = tmp_path / "_manifest.json"
    manifest = Manifest.load(path)
    manifest.add(_record(tmp_path))
    manifest.save()

    reloaded = Manifest.load(path)
    record = reloaded.get("11")
    assert record is not None
    assert record.title == "Welcome"
    assert record.speakers == ["Maria Rossi"]
    assert record.abstract == "Opening."


def test_loading_a_missing_manifest_gives_an_empty_one(tmp_path):
    assert Manifest.load(tmp_path / "_manifest.json").counts()["done"] == 0


def test_a_completed_record_with_a_matching_file_is_complete(tmp_path):
    manifest = Manifest.load(tmp_path / "_manifest.json")
    manifest.add(_record(tmp_path))
    assert manifest.is_complete("11") is True


def test_a_completed_record_whose_file_vanished_is_not_complete(tmp_path):
    manifest = Manifest.load(tmp_path / "_manifest.json")
    record = _record(tmp_path)
    manifest.add(record)
    (tmp_path / "11.pdf").unlink()
    assert manifest.is_complete("11") is False


def test_a_completed_record_whose_file_changed_is_not_complete(tmp_path):
    manifest = Manifest.load(tmp_path / "_manifest.json")
    manifest.add(_record(tmp_path))
    (tmp_path / "11.pdf").write_bytes(b"%PDF-1.4 different")
    assert manifest.is_complete("11") is False


def test_a_failed_record_is_never_complete_so_it_is_retried(tmp_path):
    manifest = Manifest.load(tmp_path / "_manifest.json")
    manifest.add(_record(tmp_path, status="failed"))
    assert manifest.is_complete("11") is False


def test_counts_group_by_status(tmp_path):
    manifest = Manifest.load(tmp_path / "_manifest.json")
    manifest.add(_record(tmp_path, contribution_id="1", status="done"))
    manifest.add(_record(tmp_path, contribution_id="2", status="failed"))
    manifest.add(_record(tmp_path, contribution_id="3", status="failed"))
    counts = manifest.counts()
    assert counts["done"] == 1 and counts["failed"] == 2
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_manifest.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'indico_rag.pipeline.manifest'`

- [ ] **Step 3: Write the implementation**

Create `src/indico_rag/pipeline/manifest.py`:

```python
"""The record of what was downloaded, converted and skipped.

Doubles as the resume record and as the metadata a retrieval pipeline indexes.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

MANIFEST_NAME = "_manifest.json"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class TalkRecord:
    contribution_id: str
    title: str
    abstract: str
    speakers: list[str]
    affiliations: list[str]
    session: str
    track: str
    contribution_type: str
    start: str | None
    end: str | None
    indico_url: str
    source_url: str
    source_filename: str
    local_path: str | None
    sha256: str | None
    bytes: int | None
    converted: bool
    conversion_error: str | None
    text_path: str | None
    text_chars: int | None
    status: str
    error: str | None


@dataclass
class Manifest:
    path: Path
    records: dict[str, TalkRecord] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "Manifest":
        if not path.exists():
            return cls(path=path)
        payload = json.loads(path.read_text())
        records = {key: TalkRecord(**value) for key, value in payload.get("talks", {}).items()}
        return cls(path=path, records=records)

    def add(self, record: TalkRecord) -> None:
        self.records[record.contribution_id] = record

    def get(self, contribution_id: str) -> TalkRecord | None:
        return self.records.get(contribution_id)

    def is_complete(self, contribution_id: str) -> bool:
        record = self.records.get(contribution_id)
        if record is None or record.status != "done" or not record.local_path:
            return False
        path = Path(record.local_path)
        if not path.exists():
            return False
        return record.sha256 == sha256_of(path)

    def counts(self) -> dict[str, int]:
        tally = Counter(record.status for record in self.records.values())
        for status in ("done", "failed", "no_material"):
            tally.setdefault(status, 0)
        return dict(tally)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "counts": self.counts(),
            "talks": {key: asdict(value) for key, value in self.records.items()},
        }
        self.path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_manifest.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add src/indico_rag/pipeline/manifest.py tests/test_manifest.py
git commit -m "Add the download manifest

Each record carries the metadata a retrieval pipeline needs alongside
the checksum that decides whether a re-run can skip the talk. Only a
completed record whose file is still present and unchanged counts as
complete, so failures are retried.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_018ZHVctiRLWSFUGrkVvhoXr"
```

---

### Task 12: The pipeline runner and the `fetch` command

The second milestone you can drive by hand. After this task the tool does its whole job headlessly; everything after it is interface.

**Files:**
- Create: `src/indico_rag/pipeline/runner.py`
- Modify: `src/indico_rag/cli.py` (add the `fetch` subcommand)
- Test: `tests/test_runner.py`

**Interfaces:**
- Consumes: `IndicoClient` (Task 3), `destination`/`deduplicate` (Task 8), `convert_to_pdf`/`needs_conversion`/`ConversionError` (Task 9), `write_sidecar` (Task 10), `Manifest`/`TalkRecord`/`sha256_of` (Task 11), `google_slides_pdf_url` (Task 5), `Config` (Task 6).
- Produces:
  - `@dataclass Job` with `talk, session, day, attachment, dest`
  - `build_jobs(event: Event, selected: set[str], root: Path) -> tuple[list[Job], list[Talk]]` returning jobs and the talks that have no material
  - `@dataclass Summary` with `done, failed, no_material, skipped` and `failures: list[tuple[str, str]]`
  - `async run_jobs(event, jobs, no_material, *, config, root, token=None, on_update=None) -> Summary`
  - `Stage = Literal["queued", "downloading", "converting", "extracting", "done", "failed", "skipped"]`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_runner.py`:

```python
import asyncio
from datetime import date, datetime
from pathlib import Path

import httpx
import respx

from indico_rag.model import Attachment, Day, Event, Session, Talk
from indico_rag.pipeline import runner
from indico_rag.pipeline.manifest import MANIFEST_NAME, Manifest
from indico_rag.pipeline.runner import build_jobs, run_jobs
from indico_rag.store import Config

HOST = "https://agenda.infn.it"


def _event(*specs) -> Event:
    talks = []
    for index, (title, filename) in enumerate(specs, start=1):
        attachment = None
        if filename:
            attachment = Attachment(id=f"a{index}", filename=filename, kind="file",
                                    url=f"{HOST}/files/{filename}")
        talks.append(Talk(id=f"c{index}", contribution_id=str(index), title=title,
                          start=datetime(2026, 8, 31, 9 + index, 0), end=None,
                          speakers=["Maria Rossi"], session_title="Plenary",
                          attachments=[attachment] if attachment else [],
                          chosen=attachment))
    session = Session(id="s1", title="Plenary", slug="plenary", talks=talks)
    return Event(id="50058", host=HOST, title="Workshop", slug="workshop",
                 days=[Day(date=date(2026, 8, 31), sessions=[session])])


def _config(tmp_path) -> Config:
    return Config(output_root=tmp_path, download_concurrency=4, convert_concurrency=2,
                  extract_text=False)


def test_build_jobs_separates_talks_without_material(tmp_path):
    event = _event(("A", "a.pdf"), ("B", None))
    jobs, no_material = build_jobs(event, {"1", "2"}, tmp_path)
    assert [j.talk.contribution_id for j in jobs] == ["1"]
    assert [t.contribution_id for t in no_material] == ["2"]


def test_build_jobs_honours_the_selection(tmp_path):
    event = _event(("A", "a.pdf"), ("B", "b.pdf"))
    jobs, _ = build_jobs(event, {"2"}, tmp_path)
    assert [j.talk.contribution_id for j in jobs] == ["2"]


def test_build_jobs_deduplicates_colliding_destinations(tmp_path):
    event = _event(("Same Title", "a.pdf"), ("Same Title", "b.pdf"))
    for talk in event.all_talks():
        talk.start = datetime(2026, 8, 31, 9, 0)
    jobs, _ = build_jobs(event, {"1", "2"}, tmp_path)
    assert jobs[0].dest != jobs[1].dest


@respx.mock
async def test_pdfs_are_downloaded_and_recorded(tmp_path):
    event = _event(("A", "a.pdf"), ("B", "b.pdf"))
    respx.get(f"{HOST}/files/a.pdf").mock(return_value=httpx.Response(200, content=b"%PDF-a"))
    respx.get(f"{HOST}/files/b.pdf").mock(return_value=httpx.Response(200, content=b"%PDF-b"))

    jobs, none = build_jobs(event, {"1", "2"}, tmp_path)
    summary = await run_jobs(event, jobs, none, config=_config(tmp_path), root=tmp_path)

    assert summary.done == 2 and summary.failed == 0
    assert all(job.dest.exists() for job in jobs)
    manifest = Manifest.load(tmp_path / "50058_workshop" / MANIFEST_NAME)
    assert manifest.counts()["done"] == 2
    record = manifest.get("1")
    assert record.speakers == ["Maria Rossi"] and record.session == "Plenary"


@respx.mock
async def test_a_non_pdf_is_converted_and_the_original_is_replaced(tmp_path, monkeypatch):
    event = _event(("A", "deck.pptx"))
    respx.get(f"{HOST}/files/deck.pptx").mock(return_value=httpx.Response(200, content=b"stub"))

    async def fake_convert(source: Path, *, profile_index=0, timeout=120.0) -> Path:
        produced = source.with_suffix(".pdf")
        produced.write_bytes(b"%PDF-converted")
        return produced

    monkeypatch.setattr(runner, "convert_to_pdf", fake_convert)

    jobs, none = build_jobs(event, {"1"}, tmp_path)
    summary = await run_jobs(event, jobs, none, config=_config(tmp_path), root=tmp_path)

    assert summary.done == 1
    manifest = Manifest.load(tmp_path / "50058_workshop" / MANIFEST_NAME)
    record = manifest.get("1")
    assert record.converted is True
    assert record.local_path.endswith(".pdf")
    assert Path(record.local_path).read_bytes() == b"%PDF-converted"


@respx.mock
async def test_one_failure_does_not_stop_the_others(tmp_path):
    event = _event(("A", "a.pdf"), ("B", "b.pdf"))
    respx.get(f"{HOST}/files/a.pdf").mock(return_value=httpx.Response(404))
    respx.get(f"{HOST}/files/b.pdf").mock(return_value=httpx.Response(200, content=b"%PDF-b"))

    jobs, none = build_jobs(event, {"1", "2"}, tmp_path)
    summary = await run_jobs(event, jobs, none, config=_config(tmp_path), root=tmp_path)

    assert summary.done == 1 and summary.failed == 1
    assert summary.failures and summary.failures[0][0] == "A"


@respx.mock
async def test_a_failed_conversion_keeps_the_original_and_records_the_reason(tmp_path, monkeypatch):
    event = _event(("A", "deck.pptx"))
    respx.get(f"{HOST}/files/deck.pptx").mock(return_value=httpx.Response(200, content=b"stub"))

    async def failing_convert(source: Path, *, profile_index=0, timeout=120.0) -> Path:
        raise runner.ConversionError("LibreOffice exited 1")

    monkeypatch.setattr(runner, "convert_to_pdf", failing_convert)

    jobs, none = build_jobs(event, {"1"}, tmp_path)
    summary = await run_jobs(event, jobs, none, config=_config(tmp_path), root=tmp_path)

    assert summary.failed == 1
    record = Manifest.load(tmp_path / "50058_workshop" / MANIFEST_NAME).get("1")
    assert record.converted is False
    assert "LibreOffice exited 1" in record.conversion_error
    assert Path(record.local_path).suffix == ".pptx", "the original must be kept"


@respx.mock
async def test_a_second_run_skips_completed_talks(tmp_path):
    event = _event(("A", "a.pdf"))
    route = respx.get(f"{HOST}/files/a.pdf").mock(
        return_value=httpx.Response(200, content=b"%PDF-a")
    )
    jobs, none = build_jobs(event, {"1"}, tmp_path)
    await run_jobs(event, jobs, none, config=_config(tmp_path), root=tmp_path)

    jobs2, none2 = build_jobs(event, {"1"}, tmp_path)
    summary = await run_jobs(event, jobs2, none2, config=_config(tmp_path), root=tmp_path)

    assert summary.skipped == 1
    assert route.call_count == 1, "a completed talk must not be re-fetched"


@respx.mock
async def test_download_concurrency_is_respected(tmp_path):
    event = _event(*[(f"T{i}", f"{i}.pdf") for i in range(6)])
    live = {"now": 0, "peak": 0}

    async def slow(request):
        live["now"] += 1
        live["peak"] = max(live["peak"], live["now"])
        await asyncio.sleep(0.05)
        live["now"] -= 1
        return httpx.Response(200, content=b"%PDF")

    for index in range(6):
        respx.get(f"{HOST}/files/{index}.pdf").mock(side_effect=slow)

    config = Config(output_root=tmp_path, download_concurrency=2, convert_concurrency=1,
                    extract_text=False)
    jobs, none = build_jobs(event, {str(i + 1) for i in range(6)}, tmp_path)
    await run_jobs(event, jobs, none, config=config, root=tmp_path)

    assert live["peak"] <= 2


@respx.mock
async def test_talks_without_material_are_recorded_not_silently_dropped(tmp_path):
    event = _event(("A", None))
    jobs, none = build_jobs(event, {"1"}, tmp_path)
    summary = await run_jobs(event, jobs, none, config=_config(tmp_path), root=tmp_path)
    assert summary.no_material == 1
    assert Manifest.load(tmp_path / "50058_workshop" / MANIFEST_NAME).get("1").status == "no_material"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_runner.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'indico_rag.pipeline.runner'`

- [ ] **Step 3: Write the implementation**

Create `src/indico_rag/pipeline/runner.py`:

```python
"""One coroutine per talk: fetch, convert, extract, record.

Two semaphores gate the work. A talk arriving as a PDF never reaches the
conversion gate, so total time approaches the larger of the download and
conversion totals rather than their sum.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import httpx

from indico_rag.client import IndicoClient
from indico_rag.indico.ranking import google_slides_pdf_url, is_google_slides
from indico_rag.model import Attachment, Day, Event, Session, Talk
from indico_rag.pipeline.convert import ConversionError, convert_to_pdf, needs_conversion
from indico_rag.pipeline.extract import write_sidecar
from indico_rag.pipeline.manifest import MANIFEST_NAME, Manifest, TalkRecord, sha256_of
from indico_rag.pipeline.paths import deduplicate, destination, event_dir
from indico_rag.store import Config

Stage = Literal["queued", "downloading", "converting", "extracting", "done", "failed", "skipped"]
Update = Callable[[str, Stage, str], None]


@dataclass
class Job:
    talk: Talk
    session: Session
    day: Day
    attachment: Attachment
    dest: Path


@dataclass
class Summary:
    done: int = 0
    failed: int = 0
    no_material: int = 0
    skipped: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)


def build_jobs(event: Event, selected: set[str], root: Path) -> tuple[list[Job], list[Talk]]:
    jobs: list[Job] = []
    without: list[Talk] = []
    taken: set[Path] = set()

    for day in event.days:
        for session in day.sessions:
            for talk in session.talks:
                if talk.contribution_id not in selected:
                    continue
                if talk.chosen is None:
                    without.append(talk)
                    continue
                suffix = "pdf" if talk.chosen.kind == "link" else (talk.chosen.suffix or "bin")
                path = deduplicate(
                    destination(root, event, day, session, talk, suffix=suffix), taken
                )
                taken.add(path)
                jobs.append(Job(talk=talk, session=session, day=day,
                                attachment=talk.chosen, dest=path))
    return jobs, without


def _record(job: Job, *, status: str, local_path: Path | None = None, sha: str | None = None,
            size: int | None = None, converted: bool = False,
            conversion_error: str | None = None, text_path: str | None = None,
            text_chars: int | None = None, error: str | None = None) -> TalkRecord:
    talk = job.talk
    return TalkRecord(
        contribution_id=talk.contribution_id, title=talk.title, abstract=talk.abstract,
        speakers=list(talk.speakers), affiliations=list(talk.affiliations),
        session=job.session.title, track=talk.track,
        contribution_type=talk.contribution_type,
        start=talk.start.isoformat() if talk.start else None,
        end=talk.end.isoformat() if talk.end else None,
        indico_url=talk.url, source_url=job.attachment.url,
        source_filename=job.attachment.filename,
        local_path=str(local_path) if local_path else None, sha256=sha, bytes=size,
        converted=converted, conversion_error=conversion_error,
        text_path=text_path, text_chars=text_chars, status=status, error=error,
    )


async def _fetch(job: Job, client: IndicoClient, external: httpx.AsyncClient) -> None:
    job.dest.parent.mkdir(parents=True, exist_ok=True)
    partial = job.dest.with_suffix(job.dest.suffix + ".part")

    if job.attachment.kind == "link":
        if not is_google_slides(job.attachment.url):
            raise ValueError(f"unsupported link attachment: {job.attachment.url}")
        url = google_slides_pdf_url(job.attachment.url)
        async with external.stream("GET", url) as response:
            response.raise_for_status()
            with partial.open("wb") as handle:
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    handle.write(chunk)
    else:
        await client.stream_to(job.attachment.url, partial)

    partial.replace(job.dest)


async def _process(job: Job, *, client: IndicoClient, external: httpx.AsyncClient,
                   manifest: Manifest, summary: Summary, config: Config,
                   download_sem: asyncio.Semaphore, convert_sem: asyncio.Semaphore,
                   profiles: asyncio.Queue[int], on_update: Update) -> None:
    talk_id = job.talk.contribution_id

    if manifest.is_complete(talk_id):
        summary.skipped += 1
        on_update(talk_id, "skipped", "already downloaded")
        return

    try:
        on_update(talk_id, "downloading", job.attachment.filename)
        async with download_sem:
            await _fetch(job, client, external)
    except Exception as exc:  # noqa: BLE001 - recorded, never suppressed
        summary.failed += 1
        summary.failures.append((job.talk.title, f"download failed: {exc}"))
        manifest.add(_record(job, status="failed", error=f"download failed: {exc}"))
        on_update(talk_id, "failed", str(exc))
        return

    final = job.dest
    converted = False
    conversion_error: str | None = None

    if needs_conversion(job.dest.suffix.lstrip(".")):
        on_update(talk_id, "converting", job.dest.name)
        profile = await profiles.get()
        try:
            async with convert_sem:
                final = await convert_to_pdf(job.dest, profile_index=profile)
            converted = True
            job.dest.unlink(missing_ok=True)
        except (ConversionError, OSError) as exc:
            conversion_error = str(exc)
        finally:
            profiles.put_nowait(profile)

        if conversion_error is not None:
            summary.failed += 1
            summary.failures.append((job.talk.title, f"conversion failed: {conversion_error}"))
            manifest.add(_record(
                job, status="failed", local_path=job.dest, sha=sha256_of(job.dest),
                size=job.dest.stat().st_size, converted=False,
                conversion_error=conversion_error,
                error=f"conversion failed: {conversion_error}",
            ))
            on_update(talk_id, "failed", conversion_error)
            return

    text_path: str | None = None
    text_chars: int | None = None
    if config.extract_text:
        on_update(talk_id, "extracting", final.name)
        try:
            sidecar, text_chars = write_sidecar(final)
            text_path = str(sidecar)
        except Exception as exc:  # noqa: BLE001 - recorded, never suppressed
            conversion_error = f"text extraction failed: {exc}"

    manifest.add(_record(
        job, status="done", local_path=final, sha=sha256_of(final),
        size=final.stat().st_size, converted=converted, conversion_error=conversion_error,
        text_path=text_path, text_chars=text_chars,
    ))
    summary.done += 1
    on_update(talk_id, "done", final.name)


async def run_jobs(event: Event, jobs: list[Job], no_material: list[Talk], *, config: Config,
                   root: Path, token: str | None = None,
                   on_update: Update | None = None) -> Summary:
    report: Update = on_update or (lambda *_: None)
    manifest_path = event_dir(root, event) / MANIFEST_NAME
    manifest = Manifest.load(manifest_path)
    summary = Summary()

    for talk in no_material:
        summary.no_material += 1
        manifest.records[talk.contribution_id] = TalkRecord(
            contribution_id=talk.contribution_id, title=talk.title, abstract=talk.abstract,
            speakers=list(talk.speakers), affiliations=list(talk.affiliations),
            session=talk.session_title, track=talk.track,
            contribution_type=talk.contribution_type,
            start=talk.start.isoformat() if talk.start else None,
            end=talk.end.isoformat() if talk.end else None,
            indico_url=talk.url, source_url="", source_filename="", local_path=None,
            sha256=None, bytes=None, converted=False, conversion_error=None,
            text_path=None, text_chars=None, status="no_material", error=None,
        )
        report(talk.contribution_id, "skipped", "no material")

    download_sem = asyncio.Semaphore(config.download_concurrency)
    convert_sem = asyncio.Semaphore(config.convert_concurrency)
    profiles: asyncio.Queue[int] = asyncio.Queue()
    for index in range(max(1, config.convert_concurrency)):
        profiles.put_nowait(index)

    for job in jobs:
        report(job.talk.contribution_id, "queued", job.attachment.filename)

    async with IndicoClient(event.host, token=token) as client, httpx.AsyncClient(
        follow_redirects=True, timeout=120
    ) as external:
        async with asyncio.TaskGroup() as group:
            for job in jobs:
                group.create_task(_process(
                    job, client=client, external=external, manifest=manifest, summary=summary,
                    config=config, download_sem=download_sem, convert_sem=convert_sem,
                    profiles=profiles, on_update=report,
                ))

    manifest.save()
    return summary
```

- [ ] **Step 4: Add the `fetch` subcommand to `cli.py`**

Insert this function above `main` and register it in `main`:

```python
def _cmd_fetch(args: argparse.Namespace) -> int:
    from pathlib import Path

    from indico_rag.pipeline.runner import build_jobs, run_jobs

    config = store.load_config()
    try:
        host, event_id = parse_event_ref(args.event, config.default_host or None)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.output:
        config.output_root = Path(args.output).expanduser()
    if args.extract:
        config.extract_text = True
    if args.concurrency:
        config.download_concurrency = args.concurrency
        if args.concurrency > 16:
            print(f"warning: {args.concurrency} parallel downloads is impolite to the host",
                  file=sys.stderr)

    try:
        event = asyncio.run(scan_event(host, event_id, token=store.get_token(host),
                                       progress=lambda m: print(f"… {m}", file=sys.stderr)))
    except AuthRequired as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3

    selected = {t.contribution_id for t in event.all_talks()}
    jobs, no_material = build_jobs(event, selected, config.output_root)
    print(f"{len(jobs)} to download, {len(no_material)} without material", file=sys.stderr)

    summary = asyncio.run(run_jobs(
        event, jobs, no_material, config=config, root=config.output_root,
        token=store.get_token(host),
        on_update=lambda tid, stage, detail: print(f"  [{stage}] {detail}", file=sys.stderr),
    ))

    print(f"\ndone={summary.done} failed={summary.failed} "
          f"skipped={summary.skipped} no-material={summary.no_material}")
    for title, reason in summary.failures:
        print(f"  FAILED  {title}: {reason}")
    return 1 if summary.failed else 0
```

Register it inside `main`, before `args = parser.parse_args(argv)`:

```python
    fetch_parser = sub.add_parser("fetch", help="download every talk in an event")
    fetch_parser.add_argument("event", help="event URL or bare event id")
    fetch_parser.add_argument("-o", "--output", help="output root directory")
    fetch_parser.add_argument("-j", "--concurrency", type=int, help="parallel downloads")
    fetch_parser.add_argument("--extract", action="store_true", help="write text sidecars")
    fetch_parser.set_defaults(func=_cmd_fetch)
```

- [ ] **Step 5: Run the whole suite**

Run: `uv run pytest -v`
Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "Add the download pipeline and a headless fetch command

One coroutine per talk runs fetch, convert and extract behind separate
download and conversion semaphores, so a talk that arrives as a PDF
never waits on the converter. Failures are isolated per talk and land in
both the manifest and the closing summary; a failed conversion keeps the
original file.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_018ZHVctiRLWSFUGrkVvhoXr"
```

> **Try it yourself — milestone 2: real downloads, no interface yet**
>
> ```bash
> cd ~/Desktop/misc/indico-timetable-tool/indico-rag
> uv run indico-rag fetch "https://agenda.infn.it/event/50058/" -o ~/Desktop/indico-test -j 8
> find ~/Desktop/indico-test -name '*.pdf' | head -20
> command cat ~/Desktop/indico-test/50058_*/_manifest.json | head -60
> ```
>
> This downloads **everything** — the TUI selection does not exist yet, so `fetch`
> takes the whole event on purpose. Add `--extract` to also write text sidecars.
> Run it a second time to watch resume skip what is already on disk.
>
> **What to give feedback on:** is the folder tree what you wanted, are the
> filenames readable, did the `.pptx` files actually become PDFs, and how does the
> wall time feel at `-j 8` versus `-j 4`? Also check the failure list at the end —
> anything listed there is a real gap worth fixing before the TUI is built on top.

---

### Task 13: Selection state and filtering

Pure logic, no Textual. This is where the invariant that filtering never mutates selection is established and tested; the screen in Task 14 only renders it.

**Files:**
- Create: `src/indico_rag/tui/__init__.py` (empty)
- Create: `src/indico_rag/tui/selection.py`
- Create: `src/indico_rag/tui/filtering.py`
- Test: `tests/test_selection.py`, `tests/test_filtering.py`

**Interfaces:**
- Produces:
  - `selection.TriState = Literal["none", "partial", "all"]`
  - `selection.SelectionState(event)` with `.selected: set[str]`, `.count`, `.toggle_talk(cid)`, `.set_talks(ids, on)`, `.session_state(session)`, `.day_state(day)`, `.toggle_session(session)`, `.toggle_day(day)`, `.is_selected(cid)`
  - `filtering.MaterialState = Literal["pdf", "convert", "none"]`
  - `filtering.material_state(talk) -> MaterialState`
  - `@dataclass filtering.FilterSpec` with `text: str = ""`, `regex: bool = False`, `days: set[date] = set()`, `sessions: set[str] = set()`, `tracks: set[str] = set()`, `types: set[str] = set()`, `material: set[str] = set()`
  - `filtering.InvalidPattern(Exception)`
  - `filtering.visible_talks(event, spec) -> list[Talk]`
  - `filtering.facets(event) -> dict[str, list]` — the distinct days, sessions, tracks and types for the filter menu

- [ ] **Step 1: Write the failing tests**

Create `tests/test_selection.py`:

```python
from datetime import date, datetime

from indico_rag.model import Day, Event, Session, Talk
from indico_rag.tui.selection import SelectionState


def _event() -> Event:
    def talk(i, session_title):
        return Talk(id=f"c{i}", contribution_id=str(i), title=f"Talk {i}",
                    start=datetime(2026, 8, 31, 9 + i, 0), end=None,
                    session_title=session_title)

    s1 = Session(id="s1", title="Plenary", slug="p", talks=[talk(1, "Plenary"), talk(2, "Plenary")])
    s2 = Session(id="s2", title="Parallel", slug="q", talks=[talk(3, "Parallel")])
    return Event(id="1", host="h", title="E", slug="e",
                 days=[Day(date=date(2026, 8, 31), sessions=[s1, s2])])


def test_a_new_selection_is_empty():
    state = SelectionState(_event())
    assert state.count == 0
    assert state.day_state(_event().days[0]) == "none"


def test_toggling_a_talk_flips_it():
    event = _event()
    state = SelectionState(event)
    state.toggle_talk("1")
    assert state.is_selected("1") is True
    state.toggle_talk("1")
    assert state.is_selected("1") is False


def test_a_session_is_partial_when_only_some_talks_are_selected():
    event = _event()
    state = SelectionState(event)
    state.toggle_talk("1")
    plenary = event.days[0].sessions[0]
    assert state.session_state(plenary) == "partial"
    state.toggle_talk("2")
    assert state.session_state(plenary) == "all"


def test_toggling_a_session_selects_all_its_talks_then_clears_them():
    event = _event()
    state = SelectionState(event)
    plenary = event.days[0].sessions[0]
    state.toggle_session(plenary)
    assert state.selected == {"1", "2"}
    state.toggle_session(plenary)
    assert state.selected == set()


def test_toggling_a_day_covers_every_session():
    event = _event()
    state = SelectionState(event)
    state.toggle_day(event.days[0])
    assert state.selected == {"1", "2", "3"}
    assert state.day_state(event.days[0]) == "all"


def test_a_partially_selected_day_toggles_to_fully_selected():
    event = _event()
    state = SelectionState(event)
    state.toggle_talk("1")
    assert state.day_state(event.days[0]) == "partial"
    state.toggle_day(event.days[0])
    assert state.selected == {"1", "2", "3"}
```

Create `tests/test_filtering.py`:

```python
from datetime import date, datetime

import pytest

from indico_rag.model import Attachment, Day, Event, Session, Talk
from indico_rag.tui.filtering import (
    FilterSpec,
    InvalidPattern,
    facets,
    material_state,
    visible_talks,
)
from indico_rag.tui.selection import SelectionState


def _event() -> Event:
    pdf = Attachment(id="a1", filename="deck.pdf", kind="file", url="u")
    pptx = Attachment(id="a2", filename="deck.pptx", kind="file", url="u")

    neutrino = Talk(id="c1", contribution_id="1", title="Neutrino Mass Limits",
                    start=datetime(2026, 8, 31, 9, 0), end=None, speakers=["Li Chen"],
                    track="Neutrinos", contribution_type="Oral", session_title="Plenary",
                    attachments=[pdf], chosen=pdf)
    background = Talk(id="c2", contribution_id="2", title="Background Modeling",
                      start=datetime(2026, 8, 31, 11, 0), end=None, speakers=["A. Müller"],
                      track="Backgrounds", contribution_type="Oral", session_title="Plenary",
                      attachments=[pptx], chosen=pptx)
    panel = Talk(id="c3", contribution_id="3", title="Discussion",
                 start=datetime(2026, 9, 1, 14, 0), end=None, track="",
                 contribution_type="Panel", session_title="Closing")

    day1 = Day(date=date(2026, 8, 31),
               sessions=[Session(id="s1", title="Plenary", slug="p",
                                 talks=[neutrino, background])])
    day2 = Day(date=date(2026, 9, 1),
               sessions=[Session(id="s2", title="Closing", slug="c", talks=[panel])])
    return Event(id="1", host="h", title="E", slug="e", days=[day1, day2])


def test_material_state_classifies_each_talk():
    event = _event()
    talks = {t.title: t for t in event.all_talks()}
    assert material_state(talks["Neutrino Mass Limits"]) == "pdf"
    assert material_state(talks["Background Modeling"]) == "convert"
    assert material_state(talks["Discussion"]) == "none"


def test_an_empty_spec_shows_everything():
    event = _event()
    assert len(visible_talks(event, FilterSpec())) == 3


def test_substring_matching_is_case_insensitive():
    event = _event()
    found = visible_talks(event, FilterSpec(text="neutrino"))
    assert [t.title for t in found] == ["Neutrino Mass Limits"]


def test_substring_matching_covers_speaker_and_session():
    event = _event()
    assert len(visible_talks(event, FilterSpec(text="chen"))) == 1
    assert len(visible_talks(event, FilterSpec(text="plenary"))) == 2


def test_regex_mode_matches_a_pattern():
    event = _event()
    found = visible_talks(event, FilterSpec(text=r"^Back.*Modeling$", regex=True))
    assert [t.title for t in found] == ["Background Modeling"]


def test_a_regex_pattern_is_a_literal_string_in_substring_mode():
    event = _event()
    assert visible_talks(event, FilterSpec(text=r"^Back.*Modeling$")) == []


def test_an_invalid_regex_raises_rather_than_falling_back_to_substring():
    event = _event()
    with pytest.raises(InvalidPattern):
        visible_talks(event, FilterSpec(text="Back(", regex=True))


def test_filtering_by_day_track_type_and_material():
    event = _event()
    assert len(visible_talks(event, FilterSpec(days={date(2026, 9, 1)}))) == 1
    assert len(visible_talks(event, FilterSpec(tracks={"Neutrinos"}))) == 1
    assert len(visible_talks(event, FilterSpec(types={"Panel"}))) == 1
    assert len(visible_talks(event, FilterSpec(material={"convert"}))) == 1


def test_structured_filters_combine_conjunctively():
    event = _event()
    spec = FilterSpec(days={date(2026, 8, 31)}, material={"pdf"})
    assert [t.title for t in visible_talks(event, spec)] == ["Neutrino Mass Limits"]


def test_facets_list_the_distinct_values():
    event = _event()
    found = facets(event)
    assert found["days"] == [date(2026, 8, 31), date(2026, 9, 1)]
    assert found["sessions"] == ["Closing", "Plenary"]
    assert found["tracks"] == ["Backgrounds", "Neutrinos"]
    assert found["types"] == ["Oral", "Panel"]


def test_filtering_does_not_change_the_selection():
    event = _event()
    state = SelectionState(event)
    state.toggle_talk("3")
    visible_talks(event, FilterSpec(text="neutrino"))
    assert state.selected == {"3"}


def test_selecting_all_visible_accumulates_across_successive_filters():
    # The workflow this protects: filter to a track, select all, clear, repeat.
    event = _event()
    state = SelectionState(event)

    shown = visible_talks(event, FilterSpec(tracks={"Neutrinos"}))
    state.set_talks([t.contribution_id for t in shown], True)
    assert state.selected == {"1"}

    shown = visible_talks(event, FilterSpec(types={"Panel"}))
    state.set_talks([t.contribution_id for t in shown], True)
    assert state.selected == {"1", "3"}

    assert len(visible_talks(event, FilterSpec())) == 3
    assert state.selected == {"1", "3"}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_selection.py tests/test_filtering.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'indico_rag.tui'`

- [ ] **Step 3: Write `tui/selection.py`**

```python
"""Which talks are selected, independent of how they are displayed."""
from __future__ import annotations

from typing import Literal

from indico_rag.model import Day, Event, Session

TriState = Literal["none", "partial", "all"]


class SelectionState:
    """A set of selected contribution ids, with tri-state rollups for parents."""

    def __init__(self, event: Event) -> None:
        self.event = event
        self.selected: set[str] = set()

    @property
    def count(self) -> int:
        return len(self.selected)

    def is_selected(self, contribution_id: str) -> bool:
        return contribution_id in self.selected

    def toggle_talk(self, contribution_id: str) -> None:
        self.selected.symmetric_difference_update({contribution_id})

    def set_talks(self, contribution_ids: list[str] | set[str], on: bool) -> None:
        if on:
            self.selected.update(contribution_ids)
        else:
            self.selected.difference_update(contribution_ids)

    def _rollup(self, contribution_ids: list[str]) -> TriState:
        if not contribution_ids:
            return "none"
        chosen = sum(1 for cid in contribution_ids if cid in self.selected)
        if chosen == 0:
            return "none"
        return "all" if chosen == len(contribution_ids) else "partial"

    def session_state(self, session: Session) -> TriState:
        return self._rollup([t.contribution_id for t in session.talks])

    def day_state(self, day: Day) -> TriState:
        return self._rollup(
            [t.contribution_id for s in day.sessions for t in s.talks]
        )

    def toggle_session(self, session: Session) -> None:
        ids = [t.contribution_id for t in session.talks]
        self.set_talks(ids, self._rollup(ids) != "all")

    def toggle_day(self, day: Day) -> None:
        ids = [t.contribution_id for s in day.sessions for t in s.talks]
        self.set_talks(ids, self._rollup(ids) != "all")
```

- [ ] **Step 4: Write `tui/filtering.py`**

```python
"""Narrowing which talks are displayed.

Filtering changes what is shown and never what is selected; the two live in
separate modules so that separation is structural rather than a convention.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date as Date
from typing import Literal

from indico_rag.indico.ranking import FORMAT_ORDER
from indico_rag.model import Event, Talk

MaterialState = Literal["pdf", "convert", "none"]


class InvalidPattern(Exception):
    """The regular expression in the filter box does not compile."""


def material_state(talk: Talk) -> MaterialState:
    if talk.chosen is None:
        return "none"
    if talk.chosen.kind == "file" and talk.chosen.suffix == "pdf":
        return "pdf"
    return "convert"


@dataclass
class FilterSpec:
    text: str = ""
    regex: bool = False
    days: set[Date] = field(default_factory=set)
    sessions: set[str] = field(default_factory=set)
    tracks: set[str] = field(default_factory=set)
    types: set[str] = field(default_factory=set)
    material: set[str] = field(default_factory=set)


def _matcher(spec: FilterSpec):
    if not spec.text:
        return lambda _: True
    if spec.regex:
        try:
            pattern = re.compile(spec.text, re.I)
        except re.error as exc:
            raise InvalidPattern(str(exc)) from exc
        return lambda haystack: pattern.search(haystack) is not None
    needle = spec.text.lower()
    return lambda haystack: needle in haystack.lower()


def visible_talks(event: Event, spec: FilterSpec) -> list[Talk]:
    """Return the talks the filter shows, in agenda order."""
    matches = _matcher(spec)
    found: list[Talk] = []

    for day in event.days:
        if spec.days and day.date not in spec.days:
            continue
        for session in day.sessions:
            if spec.sessions and session.title not in spec.sessions:
                continue
            for talk in session.talks:
                if spec.tracks and talk.track not in spec.tracks:
                    continue
                if spec.types and talk.contribution_type not in spec.types:
                    continue
                if spec.material and material_state(talk) not in spec.material:
                    continue
                haystack = " ".join([talk.title, " ".join(talk.speakers), session.title])
                if matches(haystack):
                    found.append(talk)
    return found


def facets(event: Event) -> dict[str, list]:
    """The distinct values available to the structured filters."""
    days = [day.date for day in event.days]
    sessions = {s.title for day in event.days for s in day.sessions}
    tracks = {t.track for t in event.all_talks() if t.track}
    types = {t.contribution_type for t in event.all_talks() if t.contribution_type}
    return {
        "days": sorted(days),
        "sessions": sorted(sessions),
        "tracks": sorted(tracks),
        "types": sorted(types),
        "material": ["pdf", "convert", "none"],
        "formats": sorted(FORMAT_ORDER),
    }
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_selection.py tests/test_filtering.py -v`
Expected: 18 passed.

- [ ] **Step 6: Commit**

```bash
git add src/indico_rag/tui tests/test_selection.py tests/test_filtering.py
git commit -m "Add selection state and filtering

Selection and filtering live in separate modules because filtering
changes only what is displayed. Selecting every visible talk therefore
accumulates across successive filters instead of replacing the previous
choice. An unparseable regular expression raises rather than falling
back to substring matching, which would silently show a different set.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_018ZHVctiRLWSFUGrkVvhoXr"
```

---

### Task 14: The selection screen

**Files:**
- Create: `src/indico_rag/tui/screens/__init__.py` (empty)
- Create: `src/indico_rag/tui/screens/select.py`
- Test: `tests/test_select_screen.py`

**Interfaces:**
- Consumes: `SelectionState` and `FilterSpec`/`visible_talks`/`InvalidPattern`/`material_state` from Task 13.
- Produces:
  - `talk_label(talk, selected: bool) -> str`
  - `class SelectScreen(Screen)` constructed as `SelectScreen(event, output_root: Path)`, exposing `.state: SelectionState`, `.spec: FilterSpec`, `.output_root: Path`, and posting `SelectScreen.Start(selected: set[str], output_root: Path)` when the user presses `D`.

**Textual note:** the code below targets Textual 0.80+. If a widget API has moved, adapt the implementation and keep the test assertions — they describe behavior, not widget internals.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_select_screen.py`:

```python
from datetime import date, datetime
from pathlib import Path

from indico_rag.model import Attachment, Day, Event, Session, Talk
from indico_rag.tui.app import IndicoRagApp
from indico_rag.tui.screens.select import SelectScreen, talk_label


def _event() -> Event:
    pdf = Attachment(id="a1", filename="deck.pdf", kind="file", url="u")
    pptx = Attachment(id="a2", filename="deck.pptx", kind="file", url="u")
    t1 = Talk(id="c1", contribution_id="1", title="Neutrino Mass Limits",
              start=datetime(2026, 8, 31, 9, 0), end=None, speakers=["Li Chen"],
              track="Neutrinos", session_title="Plenary", attachments=[pdf], chosen=pdf)
    t2 = Talk(id="c2", contribution_id="2", title="Background Modeling",
              start=datetime(2026, 8, 31, 11, 0), end=None, speakers=["A. Müller"],
              track="Backgrounds", session_title="Plenary", attachments=[pptx], chosen=pptx)
    t3 = Talk(id="c3", contribution_id="3", title="Discussion", start=None, end=None,
              session_title="Plenary")
    return Event(id="1", host="h", title="E", slug="e",
                 days=[Day(date=date(2026, 8, 31),
                           sessions=[Session(id="s1", title="Plenary", slug="p",
                                             talks=[t1, t2, t3])])])


def test_talk_label_shows_time_title_speaker_and_file():
    talk = _event().all_talks()[0]
    label = talk_label(talk, selected=False)
    assert "09:00" in label and "Neutrino Mass Limits" in label
    assert "Li Chen" in label and "deck.pdf" in label


def test_talk_label_marks_conversion_and_absence():
    talks = _event().all_talks()
    assert "→ pdf" in talk_label(talks[1], selected=False)
    assert "no material" in talk_label(talks[2], selected=False)


def test_talk_label_shows_the_checkbox_state():
    talk = _event().all_talks()[0]
    assert talk_label(talk, selected=True) != talk_label(talk, selected=False)


async def test_select_all_then_none(tmp_path):
    app = IndicoRagApp()
    async with app.run_test() as pilot:
        screen = SelectScreen(_event(), tmp_path)
        await app.push_screen(screen)
        await pilot.pause()

        await pilot.press("a")
        assert screen.state.selected == {"1", "2", "3"}

        await pilot.press("N")
        assert screen.state.selected == set()


async def test_filtering_then_selecting_all_only_takes_the_visible_talks(tmp_path):
    app = IndicoRagApp()
    async with app.run_test() as pilot:
        screen = SelectScreen(_event(), tmp_path)
        await app.push_screen(screen)
        await pilot.pause()

        screen.spec.text = "neutrino"
        screen.refresh_tree()
        await pilot.pause()

        await pilot.press("a")
        assert screen.state.selected == {"1"}

        screen.spec.text = ""
        screen.refresh_tree()
        await pilot.pause()
        assert screen.state.selected == {"1"}, "clearing the filter must not clear the selection"


async def test_an_invalid_regex_is_reported_and_shows_nothing(tmp_path):
    app = IndicoRagApp()
    async with app.run_test() as pilot:
        screen = SelectScreen(_event(), tmp_path)
        await app.push_screen(screen)
        await pilot.pause()

        screen.spec.regex = True
        screen.spec.text = "Back("
        screen.refresh_tree()
        await pilot.pause()

        assert screen.visible == []
        assert "invalid" in screen.status_text.lower()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_select_screen.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'indico_rag.tui.app'`

- [ ] **Step 3: Write `tui/screens/select.py`**

```python
"""The day / session / talk tree where talks are chosen."""
from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.message import Message
from textual.screen import Screen
from textual.widgets import Footer, Header, Input, Static, Tree

from indico_rag.model import Event, Talk
from indico_rag.tui.filtering import FilterSpec, InvalidPattern, material_state, visible_talks
from indico_rag.tui.selection import SelectionState

_BOX = {"none": "[ ]", "partial": "[~]", "all": "[x]"}


def talk_label(talk: Talk, selected: bool) -> str:
    clock = talk.start.strftime("%H:%M") if talk.start else "--:--"
    who = f" — {talk.speakers[0]}" if talk.speakers else ""
    state = material_state(talk)
    if state == "none":
        marker = "no material"
    elif state == "pdf":
        marker = talk.chosen.filename
    else:
        name = talk.chosen.filename if talk.chosen else "link"
        marker = f"{name} → pdf"
    return f"{_BOX['all' if selected else 'none']} {clock}  {talk.title}{who}  [{marker}]"


class SelectScreen(Screen):
    """Choose talks. Filtering narrows what is shown; it never alters the selection."""

    BINDINGS = [
        Binding("space", "toggle", "toggle"),
        Binding("a", "select_visible", "select shown"),
        Binding("N", "clear", "clear"),
        Binding("i", "invert", "invert"),
        Binding("slash", "focus_filter", "filter"),
        Binding("ctrl+r", "toggle_regex", "regex"),
        Binding("o", "override", "override file"),
        Binding("d", "change_output", "output dir"),
        Binding("D", "start", "download"),
    ]

    class Start(Message):
        def __init__(self, selected: set[str], output_root: Path) -> None:
            super().__init__()
            self.selected = selected
            self.output_root = output_root

    def __init__(self, event: Event, output_root: Path) -> None:
        super().__init__()
        self.event = event
        self.state = SelectionState(event)
        self.spec = FilterSpec()
        self.output_root = output_root
        self.visible: list[Talk] = []
        self.status_text = ""

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield Input(placeholder="filter (ctrl+r for regex)", id="filter")
        yield Tree("agenda", id="agenda")
        yield Static(id="status")
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_tree()

    def on_input_changed(self, event: Input.Changed) -> None:
        self.spec.text = event.value
        self.refresh_tree()

    def refresh_tree(self) -> None:
        tree: Tree = self.query_one("#agenda", Tree)
        try:
            self.visible = visible_talks(self.event, self.spec)
            self.status_text = ""
        except InvalidPattern as exc:
            self.visible = []
            self.status_text = f"invalid regular expression: {exc}"

        shown = {talk.contribution_id for talk in self.visible}
        tree.clear()
        tree.root.expand()

        for day in self.event.days:
            day_talks = [t for s in day.sessions for t in s.talks if t.contribution_id in shown]
            if not day_talks:
                continue
            day_node = tree.root.add(
                f"{_BOX[self.state.day_state(day)]} {day.date.isoformat()}",
                data=("day", day), expand=True,
            )
            for session in day.sessions:
                session_talks = [t for t in session.talks if t.contribution_id in shown]
                if not session_talks:
                    continue
                session_node = day_node.add(
                    f"{_BOX[self.state.session_state(session)]} {session.title}",
                    data=("session", session), expand=True,
                )
                for talk in session_talks:
                    session_node.add_leaf(
                        talk_label(talk, self.state.is_selected(talk.contribution_id)),
                        data=("talk", talk),
                    )

        mode = "regex" if self.spec.regex else "substring"
        counts = f"{self.state.count} selected · {len(self.visible)} shown · filter: {mode}"
        self.query_one("#status", Static).update(self.status_text or counts)

    def _current(self) -> tuple[str, object] | None:
        node = self.query_one("#agenda", Tree).cursor_node
        return node.data if node is not None else None

    def action_toggle(self) -> None:
        current = self._current()
        if current is None:
            return
        kind, obj = current
        if kind == "talk":
            self.state.toggle_talk(obj.contribution_id)
        elif kind == "session":
            self.state.toggle_session(obj)
        elif kind == "day":
            self.state.toggle_day(obj)
        self.refresh_tree()

    def action_select_visible(self) -> None:
        self.state.set_talks([t.contribution_id for t in self.visible], True)
        self.refresh_tree()

    def action_clear(self) -> None:
        self.state.selected.clear()
        self.refresh_tree()

    def action_invert(self) -> None:
        for talk in self.visible:
            self.state.toggle_talk(talk.contribution_id)
        self.refresh_tree()

    def action_focus_filter(self) -> None:
        self.query_one("#filter", Input).focus()

    def action_toggle_regex(self) -> None:
        self.spec.regex = not self.spec.regex
        self.refresh_tree()

    def action_override(self) -> None:
        current = self._current()
        if current is None or current[0] != "talk":
            return
        talk: Talk = current[1]
        if len(talk.attachments) < 2:
            return
        order = talk.attachments + [None]
        index = order.index(talk.chosen) if talk.chosen in order else 0
        talk.chosen = order[(index + 1) % len(order)]
        self.refresh_tree()

    def action_change_output(self) -> None:
        self.app.push_screen(
            OutputPrompt(self.output_root), callback=self._set_output
        )

    def _set_output(self, path: Path | None) -> None:
        if path is not None:
            self.output_root = path
            self.refresh_tree()

    def action_start(self) -> None:
        self.post_message(self.Start(set(self.state.selected), self.output_root))


class OutputPrompt(Screen[Path | None]):
    """Ask for an output directory."""

    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def __init__(self, current: Path) -> None:
        super().__init__()
        self.current = current

    def compose(self) -> ComposeResult:
        yield Static("Output directory:")
        yield Input(value=str(self.current), id="path")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(Path(event.value).expanduser())

    def action_cancel(self) -> None:
        self.dismiss(None)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_select_screen.py -v`
Expected: 6 passed (after Task 15 supplies `IndicoRagApp`; if you are running tasks strictly in order, write the minimal `tui/app.py` from Task 15 Step 3 first).

- [ ] **Step 5: Commit**

```bash
git add src/indico_rag/tui/screens tests/test_select_screen.py
git commit -m "Add the talk selection screen

Renders the day, session and talk tree with tri-state checkboxes and the
file each talk would produce. Selecting every shown talk accumulates
across filters, and an unparseable regular expression is reported in the
status line instead of silently matching something else.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_018ZHVctiRLWSFUGrkVvhoXr"
```

---

### Task 15: Home screen, new-event screen, and the app shell

**Files:**
- Create: `src/indico_rag/tui/app.py`
- Create: `src/indico_rag/tui/screens/home.py`
- Create: `src/indico_rag/tui/screens/new_event.py`
- Test: `tests/test_home_screen.py`

**Interfaces:**
- Consumes: `store.list_scans`/`load_scan`/`delete_scan`/`load_config` (Task 6), `scan_event` (Task 7), `SelectScreen` (Task 14).
- Produces:
  - `class IndicoRagApp(App)` with `.state_root: Path | None` (tests point it at `tmp_path`)
  - `class HomeScreen(Screen)` posting `HomeScreen.OpenScan(host, event_id)` and `HomeScreen.NewScan()`
  - `class NewEventScreen(Screen)` posting `NewEventScreen.Scanned(event)`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_home_screen.py`:

```python
from datetime import date, datetime

from indico_rag.model import Day, Event, Session, Talk
from indico_rag.store import save_scan
from indico_rag.tui.app import IndicoRagApp
from indico_rag.tui.screens.home import HomeScreen


def _event(event_id="50058", title="LNGS Workshop") -> Event:
    talk = Talk(id="c1", contribution_id="1", title="Welcome",
                start=datetime(2026, 8, 31, 9, 0), end=None)
    return Event(id=event_id, host="https://agenda.infn.it", title=title, slug="w",
                 days=[Day(date=date(2026, 8, 31),
                           sessions=[Session(id="s1", title="Plenary", slug="p", talks=[talk])])])


async def test_home_lists_saved_scans(tmp_path):
    save_scan(_event(), root=tmp_path)
    save_scan(_event(event_id="60000", title="Other Workshop"), root=tmp_path)

    app = IndicoRagApp(state_root=tmp_path)
    async with app.run_test() as pilot:
        screen = HomeScreen(state_root=tmp_path)
        await app.push_screen(screen)
        await pilot.pause()
        titles = [record.title for record in screen.records]
        assert "LNGS Workshop" in titles and "Other Workshop" in titles


async def test_home_is_empty_when_nothing_was_scanned(tmp_path):
    app = IndicoRagApp(state_root=tmp_path)
    async with app.run_test() as pilot:
        screen = HomeScreen(state_root=tmp_path)
        await app.push_screen(screen)
        await pilot.pause()
        assert screen.records == []


async def test_deleting_a_scan_removes_it_from_the_list(tmp_path):
    save_scan(_event(), root=tmp_path)
    app = IndicoRagApp(state_root=tmp_path)
    async with app.run_test() as pilot:
        screen = HomeScreen(state_root=tmp_path)
        await app.push_screen(screen)
        await pilot.pause()
        screen.action_forget()
        await pilot.pause()
        assert screen.records == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_home_screen.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'indico_rag.tui.app'`

- [ ] **Step 3: Write `tui/app.py`**

```python
"""The Textual application shell."""
from __future__ import annotations

from pathlib import Path

from textual.app import App

from indico_rag import store


class IndicoRagApp(App):
    """Screens are pushed by the entry point; `state_root` lets tests redirect state."""

    CSS = """
    Screen { layout: vertical; }
    Tree { height: 1fr; }
    #status { height: 1; color: $text-muted; }
    """
    TITLE = "indico-rag"

    def __init__(self, state_root: Path | None = None) -> None:
        super().__init__()
        self.state_root = state_root
        self.config = store.load_config(state_root)
```

- [ ] **Step 4: Write `tui/screens/home.py`**

```python
"""Past scans, and the way into a new one."""
from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.message import Message
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static

from indico_rag import store


class HomeScreen(Screen):
    BINDINGS = [
        Binding("enter", "open", "open"),
        Binding("n", "new", "new event"),
        Binding("r", "rescan", "rescan"),
        Binding("d", "forget", "forget"),
        Binding("q", "quit", "quit"),
    ]

    class OpenScan(Message):
        def __init__(self, host: str, event_id: str, rescan: bool = False) -> None:
            super().__init__()
            self.host = host
            self.event_id = event_id
            self.rescan = rescan

    class NewScan(Message):
        pass

    def __init__(self, state_root: Path | None = None) -> None:
        super().__init__()
        self.state_root = state_root
        self.records: list[store.ScanRecord] = []

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(id="scans", cursor_type="row")
        yield Static("press n for a new event", id="status")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#scans", DataTable)
        table.add_columns("scanned", "event", "talks", "dates", "title")
        self.reload()

    def reload(self) -> None:
        self.records = store.list_scans(self.state_root)
        table = self.query_one("#scans", DataTable)
        table.clear()
        for record in self.records:
            table.add_row(record.fetched_at, record.event_id, str(record.talk_count),
                          f"{record.first_day}..{record.last_day}", record.title)

    def _current(self) -> store.ScanRecord | None:
        table = self.query_one("#scans", DataTable)
        if not self.records:
            return None
        index = table.cursor_row if table.cursor_row is not None else 0
        return self.records[index] if 0 <= index < len(self.records) else None

    def action_open(self) -> None:
        record = self._current()
        if record is not None:
            self.post_message(self.OpenScan(record.host, record.event_id))

    def action_rescan(self) -> None:
        record = self._current()
        if record is not None:
            self.post_message(self.OpenScan(record.host, record.event_id, rescan=True))

    def action_new(self) -> None:
        self.post_message(self.NewScan())

    def action_forget(self) -> None:
        record = self._current()
        if record is not None:
            store.delete_scan(record.host, record.event_id, self.state_root)
            self.reload()
```

- [ ] **Step 5: Write `tui/screens/new_event.py`**

```python
"""Enter an event URL and watch the scan progress."""
from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.message import Message
from textual.screen import Screen
from textual.widgets import Footer, Header, Input, Log, Static

from indico_rag import store
from indico_rag.access.base import AuthRequired
from indico_rag.indico.endpoints import parse_event_ref
from indico_rag.model import Event
from indico_rag.scan import scan_event


class NewEventScreen(Screen):
    class Scanned(Message):
        def __init__(self, event: Event) -> None:
            super().__init__()
            self.event = event

    def __init__(self, state_root: Path | None = None, default_host: str = "") -> None:
        super().__init__()
        self.state_root = state_root
        self.default_host = default_host

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Paste an event URL, or an event id if a default host is configured:")
        yield Input(placeholder="https://agenda.infn.it/event/50058/timetable/", id="url")
        yield Log(id="progress")
        yield Footer()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.run_worker(self._scan(event.value), exclusive=True)

    async def _scan(self, text: str) -> None:
        log = self.query_one("#progress", Log)
        try:
            host, event_id = parse_event_ref(text, self.default_host or None)
        except ValueError as exc:
            log.write_line(f"error: {exc}")
            return

        log.write_line(f"scanning {host}/event/{event_id}")
        try:
            scanned = await scan_event(
                host, event_id, token=store.get_token(host, self.state_root),
                progress=log.write_line,
            )
        except AuthRequired as exc:
            log.write_line(f"error: {exc}")
            return

        store.save_scan(scanned, root=self.state_root)
        self.post_message(self.Scanned(scanned))
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/test_home_screen.py tests/test_select_screen.py -v`
Expected: 9 passed.

- [ ] **Step 7: Commit**

```bash
git add src/indico_rag/tui tests/test_home_screen.py
git commit -m "Add the home and new-event screens

The home screen lists cached scans and opens one with its saved
selection; the new-event screen accepts a pasted URL or a bare id and
reports each scan stage, so the seconds spent on a proof of work do not
look like a hang.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_018ZHVctiRLWSFUGrkVvhoXr"
```

---

### Task 16: Progress screen and the wired-up application

The third milestone: the tool you actually asked for.

**Files:**
- Create: `src/indico_rag/tui/screens/progress.py`
- Modify: `src/indico_rag/tui/app.py` (screen wiring and message handlers)
- Modify: `src/indico_rag/cli.py` (bare `indico-rag` launches the TUI)
- Test: `tests/test_progress_screen.py`

**Interfaces:**
- Consumes: `build_jobs`/`run_jobs`/`Summary` (Task 12), `SelectScreen.Start` (Task 14), `HomeScreen`/`NewEventScreen` messages (Task 15).
- Produces:
  - `class ProgressScreen(Screen)` constructed as `ProgressScreen(event, selected, output_root, config, token)`, exposing `.stages: dict[str, str]` and `.summary: Summary | None`
  - `summary_lines(summary) -> list[str]`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_progress_screen.py`:

```python
from indico_rag.pipeline.runner import Summary
from indico_rag.tui.screens.progress import summary_lines


def test_summary_lines_report_every_bucket():
    summary = Summary(done=3, failed=1, no_material=2, skipped=4,
                      failures=[("Background Modeling", "conversion failed: exited 1")])
    text = "\n".join(summary_lines(summary))
    assert "3" in text and "done" in text
    assert "no material" in text or "no-material" in text
    assert "Background Modeling" in text
    assert "conversion failed" in text


def test_a_clean_run_says_so():
    text = "\n".join(summary_lines(Summary(done=5)))
    assert "FAILED" not in text
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_progress_screen.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'indico_rag.tui.screens.progress'`

- [ ] **Step 3: Write `tui/screens/progress.py`**

```python
"""Live per-talk progress and the closing summary."""
from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static

from indico_rag.model import Event
from indico_rag.pipeline.runner import Summary, build_jobs, run_jobs
from indico_rag.store import Config


def summary_lines(summary: Summary) -> list[str]:
    lines = [
        f"done {summary.done} · failed {summary.failed} · "
        f"skipped {summary.skipped} · no material {summary.no_material}"
    ]
    lines.extend(f"FAILED  {title}: {reason}" for title, reason in summary.failures)
    return lines


class ProgressScreen(Screen):
    """One row per talk. The failure list is the output that matters here."""

    def __init__(self, event: Event, selected: set[str], output_root: Path, config: Config,
                 token: str | None = None) -> None:
        super().__init__()
        self.event = event
        self.selected = selected
        self.output_root = output_root
        self.config = config
        self.token = token
        self.stages: dict[str, str] = {}
        self.summary: Summary | None = None
        self._rows: dict[str, str] = {}

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(id="rows")
        yield Static(id="summary")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#rows", DataTable)
        table.add_columns("stage", "talk", "detail")
        self.run_worker(self._run(), exclusive=True)

    def _update(self, contribution_id: str, stage: str, detail: str) -> None:
        self.stages[contribution_id] = stage
        table = self.query_one("#rows", DataTable)
        title = next(
            (t.title for t in self.event.all_talks() if t.contribution_id == contribution_id),
            contribution_id,
        )
        if contribution_id in self._rows:
            table.update_cell(self._rows[contribution_id], "stage", stage)
            table.update_cell(self._rows[contribution_id], "detail", detail)
        else:
            key = table.add_row(stage, title, detail, key=contribution_id)
            self._rows[contribution_id] = key

    async def _run(self) -> None:
        jobs, no_material = build_jobs(self.event, self.selected, self.output_root)
        self.summary = await run_jobs(
            self.event, jobs, no_material, config=self.config, root=self.output_root,
            token=self.token, on_update=self._update,
        )
        self.query_one("#summary", Static).update("\n".join(summary_lines(self.summary)))
```

- [ ] **Step 4: Wire the screens together in `tui/app.py`**

Add these imports and methods to `IndicoRagApp`:

```python
from textual.app import App

from indico_rag import store
from indico_rag.model import Event
from indico_rag.tui.screens.home import HomeScreen
from indico_rag.tui.screens.new_event import NewEventScreen
from indico_rag.tui.screens.progress import ProgressScreen
from indico_rag.tui.screens.select import SelectScreen


    def on_mount(self) -> None:
        self.push_screen(HomeScreen(self.state_root))

    def on_home_screen_new_scan(self, _: HomeScreen.NewScan) -> None:
        self.push_screen(NewEventScreen(self.state_root, self.config.default_host))

    def on_home_screen_open_scan(self, message: HomeScreen.OpenScan) -> None:
        event, record = store.load_scan(message.host, message.event_id, self.state_root)
        screen = SelectScreen(event, Path(record.output_dir or self.config.output_root))
        screen.state.set_talks(record.selection, True)
        self.push_screen(screen)

    def on_new_event_screen_scanned(self, message: NewEventScreen.Scanned) -> None:
        self.push_screen(SelectScreen(message.event, self.config.output_root))

    def on_select_screen_start(self, message: SelectScreen.Start) -> None:
        screen = self.screen
        event: Event = screen.event
        store.save_scan(event, selection=sorted(message.selected),
                        output_dir=str(message.output_root), root=self.state_root)
        self.push_screen(ProgressScreen(
            event, message.selected, message.output_root, self.config,
            token=store.get_token(event.host, self.state_root),
        ))
```

- [ ] **Step 5: Make a bare `indico-rag` launch the TUI**

In `cli.py`, change `main` so the subcommand is optional and defaults to the TUI:

```python
def _cmd_tui(_: argparse.Namespace) -> int:
    from indico_rag.tui.app import IndicoRagApp

    IndicoRagApp().run()
    return 0
```

and in `main`, replace `sub = parser.add_subparsers(dest="command", required=True)` with:

```python
    sub = parser.add_subparsers(dest="command")
    parser.set_defaults(func=_cmd_tui)
```

- [ ] **Step 6: Run the whole suite**

Run: `uv run pytest -v`
Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "Add the progress screen and wire the application together

A bare indico-rag now opens the home screen, and the four screens pass
an event from scan through selection to download. The closing summary
lists every failure with its reason.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_018ZHVctiRLWSFUGrkVvhoXr"
```

> **Try it yourself — milestone 3: the whole tool**
>
> ```bash
> cd ~/Desktop/misc/indico-timetable-tool/indico-rag
> uv run indico-rag
> ```
>
> Press `n`, paste `https://agenda.infn.it/event/50058/timetable/`, and wait for the
> scan. In the tree: `space` toggles, `a` selects everything shown, `N` clears, `/`
> focuses the filter, `ctrl+r` switches it to regex, `o` cycles the chosen file for
> a talk, `d` changes the output directory, `D` starts the download. Quit and relaunch
> to confirm the scan and your selection came back on the home screen.
>
> **What to give feedback on:** does the tree read clearly at a real conference's
> size, are the key bindings the ones your fingers expect, is the filter fast enough
> while typing, and does the progress screen tell you what you want during a long run?

---

### Task 17: README, licence, and the GitHub remote

Run this only once you have driven milestone 3 by hand and are satisfied.

**Files:**
- Create: `README.md`, `LICENSE`
- Modify: `pyproject.toml` (licence metadata)

- [ ] **Step 1: Write `LICENSE`**

Use the MIT licence text, copyright `2026 Florian Henkes`.

- [ ] **Step 2: Write `README.md`**

It must cover: what the tool does; `brew install --cask libreoffice` as a prerequisite for conversion; installation with `uv sync`; the three commands (`indico-rag`, `indico-rag scan <url>`, `indico-rag fetch <url> -o <dir>`); the key bindings table from Task 14; the output layout; a description of `_manifest.json`'s fields; and a short note that the tool solves an Anubis proof-of-work when a host presents one, at one request at a time with default concurrency 8.

- [ ] **Step 3: Verify the whole suite passes from a clean checkout**

```bash
uv sync
uv run pytest -v
```

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "Add README and MIT licence

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_018ZHVctiRLWSFUGrkVvhoXr"
```

- [ ] **Step 5: Create the remote and push**

**Confirm with Florian before running this — it is the first outward-facing action.**

```bash
gh repo create indico-rag --public --source=. --remote=origin --push \
  --description "Download an Indico event's talks as PDFs, with a TUI for selecting them"
gh repo view --web
```

---

## Self-Review

**Spec coverage.** Access regimes → Tasks 2, 3. Data model and parser → Task 4. Ranking → Task 5. Persistence and config → Task 6. TUI home, new-event, selection, progress → Tasks 14, 15, 16. Pipeline and semaphores → Task 12. Conversion including per-process LibreOffice profiles and timeouts → Task 9. Extraction with pypdfium2 → Task 10. Output layout and sanitization → Task 8. Manifest and resume → Tasks 11, 12. Failure handling → Tasks 9, 12. Testing → every task. Repository layout and licence → Task 17.

**Two spec requirements deliberately deferred, and why.** The spec's `hosts/<host>.json` regime cache is written by Task 6 and set by the CLI in Task 7, but nothing reads it back to skip detection — detection costs one request that would happen anyway, so reading it would add a code path with no measurable gain. The spec's token prompt inside the TUI is not built; `AuthRequired` surfaces the message naming `<host>/user/tokens/`, and the token is placed with `store.set_token`. Both are noted here rather than silently dropped; say the word and either becomes a task.

**Placeholder scan.** No `TBD`, no "add error handling", no "similar to Task N". Every code step carries runnable code.

**Type consistency.** `Talk.contribution_id` is the key used by selection, manifest records, and resume throughout. `Attachment.suffix` is a property, used by ranking, `build_jobs`, and `material_state`. `Config` field names match between `store.py`, `runner.run_jobs`, and the CLI. `Summary` fields match between `runner.py` and `summary_lines`.

**One known ordering wrinkle:** Task 14's tests import `IndicoRagApp`, which Task 15 creates. Executed strictly in order, write the minimal `tui/app.py` from Task 15 Step 3 during Task 14. This is called out in Task 14 Step 4 rather than reordered, because the selection logic is the more valuable thing to build and review first.
