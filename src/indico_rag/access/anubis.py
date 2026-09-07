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
