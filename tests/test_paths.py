import json
import unicodedata
from datetime import date, datetime
from pathlib import Path

from indico_rag.indico.parser import attachments_by_contribution, parse_timetable
from indico_rag.indico.ranking import apply_ranking
from indico_rag.model import Day, Event, Session, Talk
from indico_rag.pipeline.paths import deduplicate, destination, sanitize, speaker_component

ROOT = Path("/out")
FIXTURES = Path(__file__).parent / "fixtures"


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


# --- Additional hazard tests beyond the brief's list ---


def test_destination_uses_the_sanitize_fallback_for_an_unsanitizable_title():
    event, day, session, talk = _fixture(title="???", speakers=())
    dest = destination(ROOT, event, day, session, talk)
    assert dest.name == "0900_untitled.pdf"
    assert not dest.name.startswith("_")
    assert dest.stem != ""


def test_destination_uses_the_sanitize_fallback_for_a_slash_only_title():
    event, day, session, talk = _fixture(title="///")
    dest = destination(ROOT, event, day, session, talk)
    assert "untitled" in dest.name
    assert not dest.name.startswith("_")


def test_sanitize_caps_the_encoded_byte_length_not_the_character_count():
    # Each CJK character is 3 bytes in UTF-8, so a naive character cap at 80
    # would produce a 240-byte component well past the 255-byte filesystem
    # limit.
    cjk_title = "测试" * 150  # 300 codepoints, 900 bytes if uncapped
    result = sanitize(cjk_title, max_length=80)
    assert len(result.encode("utf-8")) <= 80


def test_destination_final_component_stays_under_the_filesystem_byte_limit():
    event, day, session, talk = _fixture(
        title="测试" * 100,
        speakers=("李小明",),
        session="x" * 40,
    )
    talk.speaker_surnames = ["李小明"]
    dest = destination(ROOT, event, day, session, talk)
    assert len(dest.name.encode("utf-8")) <= 255


def test_sanitize_normalizes_nfc_and_nfd_forms_to_the_same_result():
    nfc = unicodedata.normalize("NFC", "Müller")  # composed u-umlaut
    nfd = unicodedata.normalize("NFD", "Müller")  # decomposed u + combining diaeresis
    assert nfc != nfd  # the two inputs really are different strings
    assert sanitize(nfc) == sanitize(nfd)


def test_deduplicate_is_order_independent():
    taken_a = {Path("/out/a.pdf"), Path("/out/a-2.pdf"), Path("/out/a-3.pdf")}
    taken_b = {Path("/out/a-3.pdf"), Path("/out/a.pdf"), Path("/out/a-2.pdf")}
    assert deduplicate(Path("/out/a.pdf"), taken_a) == deduplicate(Path("/out/a.pdf"), taken_b)


def test_deduplicate_terminates_through_a_long_run_of_existing_suffixes():
    taken = {Path("/out/a.pdf")} | {Path(f"/out/a-{n}.pdf") for n in range(2, 500)}
    result = deduplicate(Path("/out/a.pdf"), taken)
    assert result not in taken
    assert result == Path("/out/a-500.pdf")


def test_deduplicate_treats_differently_cased_paths_as_distinct():
    # FINDING: macOS's default filesystem (APFS/HFS+) is case-insensitive, so
    # "Talk.pdf" and "talk.pdf" name the same file there. `deduplicate` compares
    # `Path` objects, whose equality is case-sensitive, so it does not detect
    # this collision and returns the lowercase path unchanged even though the
    # uppercase variant is "taken". This is reported, not fixed, per the task
    # brief; folding case into the collision check would change behavior on
    # Linux too and needs an explicit decision.
    taken = {Path("/out/Talk.pdf")}
    assert deduplicate(Path("/out/talk.pdf"), taken) == Path("/out/talk.pdf")


def test_the_captured_event_produces_collision_free_usable_destinations():
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

    talks = event.all_talks()
    assert len(talks) == 100

    seen: set[Path] = set()
    longest_component = ""
    longest_component_talk = ""
    for day in event.days:
        for session in day.sessions:
            for talk in session.talks:
                dest = destination(ROOT, event, day, session, talk)
                deduped = deduplicate(dest, seen)
                assert deduped not in seen, f"collision producing {deduped} for talk {talk.id}"
                seen.add(deduped)

                for component in deduped.relative_to(ROOT).parts:
                    assert component != "", f"empty path component in {deduped} for talk {talk.id}"
                    encoded_length = len(component.encode("utf-8"))
                    assert encoded_length <= 255, (
                        f"{component!r} for talk {talk.id} is {encoded_length} UTF-8 bytes"
                    )
                    if encoded_length > len(longest_component.encode("utf-8")):
                        longest_component = component
                        longest_component_talk = talk.id

    print(
        f"\nlongest path component: {longest_component!r} "
        f"({len(longest_component.encode('utf-8'))} bytes, talk id={longest_component_talk})"
    )
