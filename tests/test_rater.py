"""Generic rating agent tests (no LLM required)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from contentos.agents.rater import (
    build_markdown,
    build_rating_prompt,
    clamp_score,
    generate_rating,
    is_exam_specific,
    letter_grade,
    load_rubric,
    parse_rating,
    render_markdown,
)
from contentos.core.llm import FallbackLLM, SearchBudget

PROMPT = Path(__file__).resolve().parents[1] / "prompts" / "examos-blog-rating.md"
RUBRIC = load_rubric(str(PROMPT))


def full_scores(value):
    return {criterion.key: value for criterion in RUBRIC.criteria}


def test_loads_shipped_rubric():
    assert RUBRIC.name == "ExamOS Blog Rating"
    assert len(RUBRIC.criteria) == 12
    assert RUBRIC.total_weight == 21.5
    assert RUBRIC.criteria[0].key == "factual_correctness"
    assert RUBRIC.criteria[0].weight == 3.0
    assert RUBRIC.grade_bands[0].label == "A+"
    assert RUBRIC.grade_bands[0].minimum == 4.7
    assert '"factual_correctness": 1-5' in RUBRIC.system_prompt


def test_clamp_score_bounds_and_invalid():
    assert clamp_score(3) == 3.0
    assert clamp_score(9) == 5.0
    assert clamp_score(-4) == 1.0
    assert clamp_score("nonsense") == 1.0


def test_letter_grade_bands():
    bands = RUBRIC.grade_bands
    assert letter_grade(5.0, bands) == "A+"
    assert letter_grade(4.70, bands) == "A+"
    assert letter_grade(4.69, bands) == "A"
    assert letter_grade(4.30, bands) == "A"
    assert letter_grade(4.29, bands) == "A-"
    assert letter_grade(3.99, bands) == "B+"
    assert letter_grade(3.0, bands) == "B-"
    assert letter_grade(1.0, bands) == "D-"
    assert letter_grade(0.99, bands) == "F"


def test_parse_rating_computes_total_and_grade():
    data = {
        "scores": full_scores(5),
        "strengths": ["Clear structure"],
        "weaknesses": ["Thin examples"],
        "factual_issues": [],
        "summary": "Solid.",
    }
    result = parse_rating(data, slug="post", filename="post.mdx", rubric=RUBRIC)
    assert result.total == 107.5
    assert round(result.average, 2) == 5.0
    assert result.grade == "A+"
    assert result.warnings == []


def test_parse_rating_defaults_missing_scores():
    result = parse_rating(
        {"scores": {"factual_correctness": 4}}, slug="post", filename="post.mdx", rubric=RUBRIC
    )
    assert result.scores["factual_correctness"] == 4.0
    assert result.scores["opening_strength"] == 1.0
    assert any("missing score" in warning for warning in result.warnings)


def test_parse_rating_clamps_out_of_range():
    data = {"scores": {**full_scores(3), "seo_quality": 99, "opening_strength": 0}}
    result = parse_rating(data, slug="post", filename="post.mdx", rubric=RUBRIC)
    assert result.scores["seo_quality"] == 5.0
    assert result.scores["opening_strength"] == 1.0


def test_render_markdown_shape():
    result = parse_rating(
        {
            "scores": full_scores(5),
            "strengths": ["One", "Two"],
            "weaknesses": ["Weak"],
            "factual_issues": ["[UNVERIFIABLE] pass mark"],
            "summary": "Verdict.",
        },
        slug="post",
        filename="post.mdx",
        rubric=RUBRIC,
    )
    markdown = render_markdown(result)
    assert markdown.startswith("# Rating: post.mdx")
    assert "**Grade:** A+ (5.00/5.0)" in markdown
    assert "| **Total** | | **21.5** | | **107.5** |" in markdown
    assert "**Weighted:** 107.5 / 21.5 = 5.00" in markdown
    assert "1. One" in markdown
    assert "- [UNVERIFIABLE] pass mark" in markdown


def test_build_markdown_separates_blocks():
    first = parse_rating({"scores": full_scores(5)}, slug="a", filename="a.mdx", rubric=RUBRIC)
    second = parse_rating({"scores": full_scores(4)}, slug="b", filename="b.mdx", rubric=RUBRIC)
    combined = build_markdown([first, second])
    assert combined.count("# Rating:") == 2
    assert "\n\n---\n\n" in combined


class FakeResponse:
    def __init__(self, content):
        self.content = content


class FakeLLM:
    def __init__(self, payload):
        self.payload = payload

    def invoke(self, messages):
        return FakeResponse(json.dumps(self.payload) if isinstance(self.payload, dict) else self.payload)


def _doc(slug="post", frontmatter="", body="Body"):
    return SimpleNamespace(path=f"{slug}.mdx", frontmatter=frontmatter, body=body)


def test_generate_rating_searches_only_when_unverifiable():
    first = {
        "scores": full_scores(4),
        "factual_issues": ["[UNVERIFIABLE] pass mark"],
        "summary": "First pass.",
    }
    verified = {"scores": full_scores(5), "factual_issues": [], "summary": "Verified."}
    result = generate_rating(
        _doc(), "post", FakeLLM(first), RUBRIC, search_llm=FakeLLM(verified)
    )
    assert result.searched is True
    assert result.grade == "A+"
    assert result.summary == "Verified."


def test_generate_rating_skips_search_without_unverifiable():
    clean = {"scores": full_scores(5), "factual_issues": [], "summary": "Clean."}
    result = generate_rating(_doc(), "post", FakeLLM(clean), RUBRIC, search_llm=FakeLLM(clean))
    assert result.searched is False


class _BoomLLM:
    def invoke(self, messages):
        raise AssertionError("primary LLM should not be used for exam-specific posts")


def test_exam_specific_post_searches_on_first_pass():
    verified = {"scores": full_scores(5), "factual_issues": [], "summary": "Searched."}
    doc = _doc("how-hard-is-ai-103", frontmatter='title: "How Hard Is AI-103?"')
    result = generate_rating(
        doc, "how-hard-is-ai-103", _BoomLLM(), RUBRIC, search_llm=FakeLLM(verified)
    )
    assert result.searched is True
    assert result.summary == "Searched."


class _TimeoutPrimary:
    def invoke(self, messages):
        raise type("DeadlineExceeded", (Exception,), {})()


def test_search_fallback_is_not_reported_as_searched():
    # A grounded call that times out and falls back to DeepSeek (no Google Search) must
    # not be reported as verified.
    verified = {"scores": full_scores(5), "factual_issues": [], "summary": "Fallback."}
    search_llm = FallbackLLM(_TimeoutPrimary(), FakeLLM(verified))
    doc = _doc("how-hard-is-ai-103", frontmatter='title: "How Hard Is AI-103?"')
    result = generate_rating(doc, "how-hard-is-ai-103", _BoomLLM(), RUBRIC, search_llm=search_llm)
    assert result.searched is False
    assert result.summary == "Fallback."
    assert any("grounding was unavailable" in warning for warning in result.warnings)


def test_force_ground_rates_every_document_with_search():
    # A profile with ground: always (study-plan/exam-faq) grounds even a generic doc.
    verified = {"scores": full_scores(5), "factual_issues": [], "summary": "Searched."}
    result = generate_rating(
        _doc(), "post", _BoomLLM(), RUBRIC, search_llm=FakeLLM(verified), force_ground=True
    )
    assert result.searched is True
    assert result.summary == "Searched."


def test_is_exam_specific_signals():
    assert is_exam_specific(
        "how-hard-is-ai-103", _doc("how-hard-is-ai-103", 'title: "How Hard Is AI-103?"')
    )
    assert is_exam_specific(
        "why-technical-experts-fail-cissp",
        _doc("why-technical-experts-fail-cissp", 'title: "Why Technical Experts Fail the CISSP"'),
    )
    assert is_exam_specific(
        "azure-vs-aws-vs-gcp", _doc("azure-vs-aws-vs-gcp", 'title: "Azure vs AWS vs GCP"')
    )
    assert not is_exam_specific(
        "do-practice-exams-work", _doc("do-practice-exams-work", 'title: "Do Practice Exams Work?"')
    )
    assert not is_exam_specific(
        "certifications-worth-it-ai-era",
        _doc("certifications-worth-it-ai-era", 'title: "Are Certifications Worth It?"'),
    )


def test_exam_addendum_is_appended_to_prompt():
    doc = _doc("how-hard-is-ai-103", frontmatter='title: "How Hard Is AI-103?"')
    assert "official" in build_rating_prompt("how-hard-is-ai-103", doc, exam_specific=True).lower()
    assert "official" not in build_rating_prompt("post", _doc(), exam_specific=False).lower()


def test_generate_rating_exhausted_budget_skips_exam_search():
    verified = {"scores": full_scores(5), "factual_issues": [], "summary": "Searched."}
    plain = {"scores": full_scores(4), "factual_issues": [], "summary": "Plain."}
    budget = SearchBudget(1)
    assert budget.try_acquire() is True  # spend the only grounded call
    doc = _doc("how-hard-is-ai-103", frontmatter='title: "How Hard Is AI-103?"')
    result = generate_rating(
        doc,
        "how-hard-is-ai-103",
        FakeLLM(plain),
        RUBRIC,
        search_llm=FakeLLM(verified),
        search_budget=budget,
    )
    assert result.searched is False
    assert result.summary == "Plain."
    assert any("budget exhausted" in warning for warning in result.warnings)


def test_generate_rating_shares_budget_across_posts():
    verified = {"scores": full_scores(5), "factual_issues": [], "summary": "Searched."}
    plain = {"scores": full_scores(4), "factual_issues": [], "summary": "Plain."}
    budget = SearchBudget(1)
    doc = _doc("how-hard-is-ai-103", frontmatter='title: "How Hard Is AI-103?"')
    first = generate_rating(
        doc,
        "how-hard-is-ai-103",
        FakeLLM(plain),
        RUBRIC,
        search_llm=FakeLLM(verified),
        search_budget=budget,
    )
    second = generate_rating(
        doc,
        "how-hard-is-ai-103",
        FakeLLM(plain),
        RUBRIC,
        search_llm=FakeLLM(verified),
        search_budget=budget,
    )
    assert first.searched is True
    assert second.searched is False
    assert budget.used == 1


def test_generate_rating_budget_blocks_unverifiable_second_pass():
    first = {
        "scores": full_scores(4),
        "factual_issues": ["[UNVERIFIABLE] pass mark"],
        "summary": "First pass.",
    }
    budget = SearchBudget(1)
    assert budget.try_acquire() is True  # spend the only grounded call
    result = generate_rating(
        _doc(),
        "post",
        FakeLLM(first),
        RUBRIC,
        search_llm=FakeLLM(first),
        search_budget=budget,
    )
    assert result.searched is False
    assert any("budget exhausted" in warning for warning in result.warnings)


def test_custom_rubric_from_prompt_file(tmp_path):
    prompt = tmp_path / "custom.md"
    prompt.write_text(
        "---\n"
        "name: Custom Rubric\n"
        "criteria:\n"
        "  - {key: clarity, label: Clarity, weight: 1}\n"
        "  - {key: accuracy, label: Accuracy, weight: 3}\n"
        "grade_bands:\n"
        "  - {min: 4.5, label: PASS}\n"
        "  - {min: 0.0, label: FAIL}\n"
        "---\n"
        "Grade the content.\n",
        encoding="utf-8",
    )
    rubric = load_rubric(str(prompt))
    assert rubric.name == "Custom Rubric"
    assert rubric.total_weight == 4.0

    result = parse_rating(
        {"scores": {"clarity": 5, "accuracy": 3}}, slug="x", filename="x.md", rubric=rubric
    )
    assert result.total == 14.0
    assert result.average == 3.5
    assert result.grade == "FAIL"
    assert '"clarity": 1-5' in rubric.system_prompt
