import json
import stat
from datetime import date, datetime
from pathlib import Path

from indico_rag.indico.parser import attachments_by_contribution, parse_timetable
from indico_rag.indico.ranking import apply_ranking
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

FIXTURES = Path(__file__).parent / "fixtures"


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


def test_config_with_backslashes_and_spaces_round_trips(tmp_path):
    # A Windows-style path embeds backslashes, which are escape introducers in
    # a TOML basic string; an unescaped writer would emit invalid TOML.
    original = Config(output_root=Path(r"C:\Users\A User\My Talks"),
                      download_concurrency=8, convert_concurrency=2,
                      extract_text=False, default_host='host "with" quotes')
    save_config(original, root=tmp_path)
    assert load_config(root=tmp_path) == original


def test_event_round_trips_through_json():
    restored = event_from_jsonable(event_to_jsonable(_event()))
    talk = restored.all_talks()[0]
    assert talk.title == "Welcome"
    assert talk.speaker_surnames == ["Colomer Rossi"], "surnames must survive a round trip"
    assert talk.start == datetime(2026, 8, 31, 9, 0)
    assert restored.days[0].date == date(2026, 8, 31)
    assert talk.chosen is not None and talk.chosen.filename == "deck.pdf"


def test_a_missing_chosen_id_rehydrates_as_none():
    data = event_to_jsonable(_event())
    data["days"][0]["sessions"][0]["talks"][0]["chosen"] = "does-not-exist"
    restored = event_from_jsonable(data)
    assert restored.all_talks()[0].chosen is None


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


def test_saving_a_second_scan_keeps_the_first_in_the_index(tmp_path):
    save_scan(_event(), root=tmp_path)
    other = _event()
    other.id = "60000"
    save_scan(other, root=tmp_path)
    ids = {r.event_id for r in list_scans(root=tmp_path)}
    assert ids == {"50058", "60000"}


def test_resaving_the_same_event_updates_rather_than_duplicates(tmp_path):
    save_scan(_event(), selection=[], root=tmp_path)
    save_scan(_event(), selection=["c1"], root=tmp_path)
    records = list_scans(root=tmp_path)
    assert len(records) == 1
    assert records[0].selection == ["c1"]


def test_deleting_a_scan_removes_it_from_the_index(tmp_path):
    save_scan(_event(), root=tmp_path)
    delete_scan("https://agenda.infn.it", "50058", root=tmp_path)
    assert list_scans(root=tmp_path) == []


def test_deleting_a_scan_leaves_sibling_scans_intact(tmp_path):
    save_scan(_event(), root=tmp_path)
    other = _event()
    other.id = "60000"
    save_scan(other, root=tmp_path)

    delete_scan("https://agenda.infn.it", "50058", root=tmp_path)

    ids = {r.event_id for r in list_scans(root=tmp_path)}
    assert ids == {"60000"}
    event, _record = load_scan("https://agenda.infn.it", "60000", root=tmp_path)
    assert event.all_talks()[0].title == "Welcome"


def test_tokens_round_trip_and_the_file_is_owner_only(tmp_path):
    set_token("https://indico.cern.ch", "secret", root=tmp_path)
    assert get_token("https://indico.cern.ch", root=tmp_path) == "secret"
    assert get_token("https://other.example", root=tmp_path) is None
    mode = (tmp_path / "credentials.json").stat().st_mode
    assert stat.S_IMODE(mode) == 0o600


def test_set_token_narrows_a_pre_existing_world_readable_file(tmp_path):
    path = tmp_path / "credentials.json"
    path.write_text(json.dumps({"old.example": "old-secret"}))
    path.chmod(0o644)

    set_token("https://indico.cern.ch", "secret", root=tmp_path)

    mode = path.stat().st_mode
    assert stat.S_IMODE(mode) == 0o600
    assert get_token("https://old.example", root=tmp_path) == "old-secret"
    assert get_token("https://indico.cern.ch", root=tmp_path) == "secret"


def test_a_scan_file_never_contains_a_token(tmp_path):
    set_token("https://agenda.infn.it", "secret", root=tmp_path)
    save_scan(_event(), root=tmp_path)
    for path in (tmp_path / "scans").rglob("*.json"):
        assert "secret" not in path.read_text()


def test_saving_and_reloading_a_real_scan_round_trips_the_full_event(tmp_path):
    # Synthetic single-talk fixtures cannot catch a scaling or ordering
    # problem; this exercises the round trip the application actually does.
    timetable = json.loads((FIXTURES / "timetable_50058.json").read_text())
    export = json.loads((FIXTURES / "event_50058_contributions.json").read_text())
    event = parse_timetable(
        timetable,
        host="https://agenda.infn.it",
        event_id="50058",
        event_title="captured",
        attachments=attachments_by_contribution(export),
    )
    apply_ranking(event)
    original_talks = event.all_talks()
    assert len(original_talks) == 100
    chosen_talk = next(t for t in original_talks if t.chosen is not None)

    save_scan(event, root=tmp_path)
    reloaded, _record = load_scan("https://agenda.infn.it", "50058", root=tmp_path)
    talks = reloaded.all_talks()

    assert len(talks) == 100
    surnames = {s for t in talks for s in t.speaker_surnames}
    assert "De la Torre Luque" in surnames, "a multi-word surname must survive the round trip"

    reloaded_chosen = next(t for t in talks if t.id == chosen_talk.id)
    assert reloaded_chosen.chosen is not None
    assert reloaded_chosen.chosen.filename == chosen_talk.chosen.filename
