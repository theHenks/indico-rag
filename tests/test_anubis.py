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


def test_parse_rejects_a_present_but_malformed_base_prefix_block():
    page = (
        '<script id="anubis_challenge" type="application/json">'
        '{"rules": {"difficulty": 4}, '
        '"challenge": {"id": "abc", "randomData": "deadbeef"}}</script>'
        '<script id="anubis_base_prefix" type="application/json">'
        "not valid json</script>"
    )
    with pytest.raises(ChallengeParseError):
        parse_challenge(page)


def test_solve_returns_a_digest_with_the_required_zero_prefix():
    digest, nonce = solve("abc123", 3)
    assert digest.startswith("000")
    assert hashlib.sha256(f"abc123{nonce}".encode()).hexdigest() == digest


def test_solve_gives_up_rather_than_spinning_forever():
    with pytest.raises(ProofOfWorkTooHard):
        solve("abc123", 8, max_attempts=5000)
