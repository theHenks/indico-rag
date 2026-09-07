import hashlib
import json

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


# --- is_complete: each of the three conditions gets its own dedicated test ---


def test_is_complete_requires_status_done(tmp_path):
    manifest = Manifest.load(tmp_path / "_manifest.json")
    manifest.add(_record(tmp_path, status="no_material"))
    assert manifest.is_complete("11") is False


def test_is_complete_requires_local_path_to_exist(tmp_path):
    manifest = Manifest.load(tmp_path / "_manifest.json")
    manifest.add(_record(tmp_path, status="done"))
    (tmp_path / "11.pdf").unlink()
    assert manifest.is_complete("11") is False


def test_is_complete_requires_sha256_to_match(tmp_path):
    manifest = Manifest.load(tmp_path / "_manifest.json")
    manifest.add(_record(tmp_path, status="done"))
    (tmp_path / "11.pdf").write_bytes(b"tampered content, same length!!")
    assert manifest.is_complete("11") is False


def test_is_complete_true_when_status_done_file_present_and_hash_matches(tmp_path):
    manifest = Manifest.load(tmp_path / "_manifest.json")
    manifest.add(_record(tmp_path, status="done"))
    assert manifest.is_complete("11") is True


def test_is_complete_false_for_unknown_contribution_id(tmp_path):
    manifest = Manifest.load(tmp_path / "_manifest.json")
    assert manifest.is_complete("does-not-exist") is False


# --- sha256_of streams rather than slurping the whole file into memory ---


def test_sha256_of_matches_hashlib_over_a_file_larger_than_one_chunk(tmp_path):
    path = tmp_path / "big.pdf"
    # One chunk is 1 MiB (1 << 20); write more than that so streaming is exercised.
    data = bytes((i % 256) for i in range(3 * (1 << 20) + 12345))
    path.write_bytes(data)
    assert sha256_of(path) == hashlib.sha256(data).hexdigest()


def test_sha256_of_streams_in_chunks_rather_than_reading_the_whole_file_at_once(tmp_path, monkeypatch):
    path = tmp_path / "big.pdf"
    data = bytes((i % 256) for i in range(3 * (1 << 20) + 1))
    path.write_bytes(data)

    read_sizes = []
    real_open = type(path).open

    def spy_open(self, *args, **kwargs):
        handle = real_open(self, *args, **kwargs)
        real_read = handle.read

        def spy_read(size=-1):
            read_sizes.append(size)
            return real_read(size)

        handle.read = spy_read
        return handle

    monkeypatch.setattr(type(path), "open", spy_open)
    sha256_of(path)

    assert len(read_sizes) > 1
    assert all(size > 0 and size < len(data) for size in read_sizes[:-1])


# --- manifest round-trip fidelity with non-ASCII, human-readable text ---


def test_manifest_round_trips_non_ascii_names_and_is_utf8_on_disk(tmp_path):
    path = tmp_path / "_manifest.json"
    manifest = Manifest.load(path)
    record = _record(tmp_path)
    record.speakers = ["Colomer Molla", "De la Torre Luque", "Franco Abellán"]
    record.title = "Búsqueda de neutrinos estériles"
    manifest.add(record)
    manifest.save()

    raw = path.read_bytes()
    # Written as UTF-8, not escaped ASCII: the accented characters appear
    # literally rather than as "\uXXXX" escapes.
    assert "Abellán".encode("utf-8") in raw
    assert b"\\u00e1" not in raw

    reloaded = Manifest.load(path)
    reloaded_record = reloaded.get("11")
    assert reloaded_record.speakers == ["Colomer Molla", "De la Torre Luque", "Franco Abellán"]
    assert reloaded_record.title == "Búsqueda de neutrinos estériles"


# --- counts() reports every status present, including unexpected ones ---


def test_counts_reports_an_unexpected_status_rather_than_dropping_it(tmp_path):
    manifest = Manifest.load(tmp_path / "_manifest.json")
    manifest.add(_record(tmp_path, status="weird_status"))
    counts = manifest.counts()
    assert counts.get("weird_status") == 1
