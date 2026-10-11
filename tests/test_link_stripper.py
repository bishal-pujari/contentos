"""Link stripper tests (deterministic, no network)."""

from __future__ import annotations

from contentos.agents.link_stripper import strip_frontmatter_embeds, strip_links
from contentos.core.loaders import parse_document


def _strip(body: str) -> str:
    new, _notes = strip_links(parse_document(body))
    return new


def test_inline_link_embedded_in_text_keeps_anchor():
    body = "See [ExamOS](/exams/foo) for scenario-based practice.\n"
    out = _strip(body)
    assert "ExamOS for scenario-based practice." in out
    assert "](" not in out


def test_malformed_link_with_space_between_brackets_and_paren():
    # Generated content sometimes emits "[anchor] (/path)"; unlink it too.
    out = _strip("Read [Top 10 Networking Concepts] (blog/az-104-networking) next.\n")
    assert "Read Top 10 Networking Concepts next." in out
    assert "[" not in out and "blog/" not in out


def test_angle_bracketed_url_with_spaces_is_unlinked():
    out = _strip("A [link](</exams/foo bar>) here.\n")
    assert out.strip() == "A link here."


def test_bare_root_relative_url_is_removed():
    body = "Ready? (/exams/microsoft-certified-azure-ai-apps-and-agents-developer-associate-ai-103) offers practice.\n"
    out = _strip(body)
    assert "/exams/" not in out
    assert "()" not in out
    assert "Ready? offers practice." in out


def test_bare_http_url_is_removed_but_sentence_survives():
    out = _strip("Read more at https://exam.os/guide today.\n")
    assert "https://" not in out
    assert "Read more at today." in out


def test_relative_url_fragments_like_fractions_are_untouched():
    out = _strip("Ratio 24/7 and a/b tests are common.\n")
    assert "24/7" in out
    assert "a/b" in out


def test_inline_code_is_protected():
    body = 'Use `192.168.1.100 "GET /admin/config.php?x=1"` in the log.\n'
    assert _strip(body) == body


def test_fenced_code_block_is_untouched():
    body = "# H\n\n```\n[link](/exams/foo) GET /admin/config.php\n```\n\n[real](/exams/bar)\n"
    out = _strip(body)
    assert "```\n[link](/exams/foo) GET /admin/config.php\n```" in out
    assert "[real](/exams/bar)" not in out


def test_standalone_link_line_is_removed():
    out = _strip("# H\n\n[Exam Overview : AI-103](/exams/foo)\n\nBody.\n")
    assert "/exams/foo" not in out
    assert "Body." in out


def test_label_block_is_removed():
    body = "# H\n\nRelated Exams:\n\n[A](/a)\n[B](/b)\n\nBody.\n"
    out = _strip(body)
    assert "Related Exams:" not in out
    assert "[A](/a)" not in out
    assert "Body." in out


def test_embed_component_is_removed():
    out = _strip('# H\n\n<PracticeExamEmbed examSlug="x" />\n\nBody.\n')
    assert "PracticeExamEmbed" not in out
    assert "Body." in out


def test_bulleted_embed_component_is_removed():
    body = (
        "# H\n\n"
        '  - <PracticeExamEmbed examSlug="aws-certified-ai-practitioner-aif-c01"/>\n'
        '      - <PracticeExamEmbed examSlug="aws-certified-ai-practitioner-aif-c01" />\n'
        "Body.\n"
    )
    out = _strip(body)
    assert "PracticeExamEmbed" not in out
    assert "examSlug" not in out
    assert "Body." in out


def test_frontmatter_embed_lines_are_removed():
    frontmatter = (
        "slug: x\n"
        "steps:\n"
        "  - title: Step 1\n"
        "    notes:\n"
        "      - Keep this note.\n"
        '      - <PracticeExamEmbed examSlug="aws-certified-ai-practitioner-aif-c01"/>\n'
        "    examCodes:\n"
        "      - aif-c01\n"
    )
    new, notes = strip_frontmatter_embeds(frontmatter)
    assert "PracticeExamEmbed" not in new
    assert "Keep this note." in new
    assert "examCodes:" in new and "aif-c01" in new
    assert any("frontmatter" in note for note in notes)


def test_frontmatter_without_embeds_is_untouched():
    frontmatter = "slug: x\nsteps:\n  - title: Step 1\n"
    assert strip_frontmatter_embeds(frontmatter) == (frontmatter, [])
