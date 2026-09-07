import json
from datetime import datetime
from pathlib import Path

from indico_rag.indico.parser import attachments_by_contribution, parse_timetable
from indico_rag.indico.ranking import apply_ranking, choose, google_slides_pdf_url, is_google_slides
from indico_rag.model import Attachment, Talk

FIXTURES = Path(__file__).parent / "fixtures"

RANKABLE_SUFFIXES = {"pdf", "pptx", "ppt", "odp", "key"}


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


def test_ranking_the_captured_event_picks_a_usable_file_for_nearly_every_talk():
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

    def has_rankable_attachment(talk: Talk) -> bool:
        return any(
            (a.kind == "file" and a.suffix in RANKABLE_SUFFIXES)
            or (a.kind == "link" and is_google_slides(a.url))
            for a in talk.attachments
        )

    for talk in talks:
        if has_rankable_attachment(talk):
            assert talk.chosen is not None, \
                f"talk {talk.contribution_id!r} has a rankable attachment but chose nothing"

    for talk in talks:
        chosen = talk.chosen
        if chosen is None:
            continue
        if chosen.kind == "link":
            assert is_google_slides(chosen.url)
        else:
            assert chosen.suffix in RANKABLE_SUFFIXES

    chosen_pdf = [t for t in talks if t.chosen and t.chosen.suffix == "pdf"]
    needs_conversion = [
        t for t in talks if t.chosen and t.chosen.kind == "file" and t.chosen.suffix != "pdf"
    ]
    # Measured against the captured INFN NOW 2026 event (100 talks, 111
    # attachments): 97 talks choose a PDF outright, and 2 need pptx->pdf
    # conversion (the other 2 of the event's 4 pptx files lose to a pdf
    # sibling in the same talk). Contribution 304612 ("Announcements") has
    # no attachments in either source and is the only talk left unchosen.
    assert len(chosen_pdf) == 97
    assert len(needs_conversion) == 2
