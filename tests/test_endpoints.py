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
