"""Offline tests for the team-feedback loop."""

from arbus import feedback


def test_empty_when_missing(tmp_path):
    assert feedback.load_feedback(tmp_path / "nope.md") == ""


def test_strips_comments_and_headings(tmp_path):
    p = tmp_path / "feedback.md"
    p.write_text(
        "<!-- instructions we do NOT want sent to the model -->\n"
        "# a heading, ignored\n"
        "\n"
        "- mažiau ekonomikos rinkų\n"
        "- daugiau TikTok temų\n",
        encoding="utf-8",
    )
    out = feedback.load_feedback(p)
    assert "instructions" not in out and "heading" not in out
    assert out == "- mažiau ekonomikos rinkų\n- daugiau TikTok temų"


def test_append_creates_file_with_starter(tmp_path):
    p = tmp_path / "feedback.md"
    line = feedback.append_feedback("stop pension markets", p)
    assert line.endswith("stop pension markets")
    assert p.exists()
    # starter comment is present but stripped from what the model sees
    assert "TEAM FEEDBACK" in p.read_text(encoding="utf-8")
    assert feedback.load_feedback(p) == line


def test_append_collapses_whitespace_and_ignores_empty(tmp_path):
    p = tmp_path / "feedback.md"
    assert feedback.append_feedback("   ", p) == ""
    assert not p.exists()  # nothing written for an empty note
    line = feedback.append_feedback("more   TikTok\n  stuff", p)
    assert line.endswith("more TikTok stuff")


def test_feedback_block_graceful_when_empty():
    assert "no team feedback" in feedback.feedback_block("")
    assert feedback.feedback_block("- less econ") == "- less econ"
