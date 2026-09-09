"""Progress screen for the indico-rag TUI."""

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
