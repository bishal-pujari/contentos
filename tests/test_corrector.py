"""Corrector agent tests (no network)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from contentos.agents.corrector import (
    FAQ_HEADING,
    SLOP_MAX_GROWTH,
    SLOP_MIN_SHRINK,
    count_faq_questions,
    fields_needing_rewrite,
    generate_correction,
    get_field,
    has_faq_heading,
    keyword_in_first_words,
    load_correction_spec,
    normalize_faq_heading,
    remove_truncated_endings,
    replace_faq_section,
    set_field,
)
from contentos.agents.rater import load_rubric
from contentos.core import pipeline, reporting

PROMPT = Path(__file__).resolve().parents[1] / "prompts" / "examos-blog-corrections.md"
SPEC = load_correction_spec(str(PROMPT))


class Response:
    def __init__(self, content):
        self.content = content


class RoutingLLM:
    """Routes the fact/FAQ/grammar/de-slop/ExamOS/rating prompts to their payloads."""

    def __init__(self, seo=None, faq=None, facts=None, grammar=None, slop=None, examos=None, rating=None):
        self.seo = seo if seo is not None else {}
        self.faq = faq if faq is not None else {}
        self.facts = facts if facts is not None else {}
        self.grammar = grammar if grammar is not None else {}
        self.slop = slop if slop is not None else {}
        self.examos = examos if examos is not None else {}
        self.rating = rating if rating is not None else {}
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        text = " ".join(getattr(m, "content", "") for m in messages)
        if "FACT TASK" in text:
            payload = self.facts
        elif "FAQ TASK" in text:
            payload = self.faq
        elif "DE-SLOP TASK" in text:
            payload = self.slop
        elif "EXAMOS TASK" in text:
            payload = self.examos
        elif "GRAMMAR TASK" in text:
            payload = self.grammar
        elif "SCORING CRITERIA" in text:
            payload = self.rating
        else:
            payload = self.seo
        return Response(json.dumps(payload))


def _doc(slug="azure-guide", frontmatter=None, body=None):
    fm = frontmatter
    if fm is None:
        fm = (
            f"slug: {slug}\n"
            'title: "Azure Guide"\n'
            'seoTitle: "Azure Guide"\n'
            'seoDescription: "Azure Guide"\n'
            'author: "A"\n'
            'categories: ["azure"]'
        )
    if body is None:
        body = "# Azure Guide\n\nAzure is a cloud platform.\n"
    return SimpleNamespace(path=f"{slug}.mdx", frontmatter=fm, body=body, has_frontmatter=True)


def test_spec_loads():
    assert SPEC.name == "ExamOS Blog Corrections"
    assert SPEC.title_max == 65
    assert SPEC.seo_description_max == 160
    assert SPEC.faq_questions == 4


def test_set_field_preserves_other_fields():
    fm = 'slug: x\ntitle: "Old"\nauthor: "A"\ncategories: ["a"]'
    new = set_field(fm, "title", "New Title")
    assert 'title: "New Title"' in new
    assert '"Old"' not in new
    assert 'author: "A"' in new
    assert 'categories: ["a"]' in new
    assert new.count("\ntitle:") == 1


def test_set_field_escapes_quotes():
    new = set_field("slug: x", "seoTitle", 'He said "hi"')
    assert get_field(new, "seoTitle") == 'He said "hi"'


def test_normalize_faq_heading_variants():
    for variant in ("## FAQ", "## Frequently Asked Questions", "### Fast Answers"):
        body = f"# H1\n\n{variant}\n\n### Q\n\nA\n"
        out, changed = normalize_faq_heading(body)
        assert changed
        h2 = [line for line in out.splitlines() if line.startswith("## ")]
        assert h2 == [FAQ_HEADING]
        assert variant not in out.splitlines()
    already = f"# H1\n\n{FAQ_HEADING}\n"
    out, changed = normalize_faq_heading(already)
    assert not changed and out == already


def test_remove_truncated_endings():
    body = "# H1\n\nText.\n\nOther related exams:\n"
    out, removed = remove_truncated_endings(body)
    assert removed == 1
    assert "Other related exams" not in out
    assert out.endswith("Text.\n\n")


def test_count_and_replace_faq_section():
    body = "# H\n\n## FAQ\n\n### A?\n\nx\n\n### B?\n\ny\n\n## Next\n\ntext\n"
    assert count_faq_questions(body) == 2
    new = replace_faq_section(
        body, "## Frequently Asked Questions (FAQ)\n\n### New?\n\nN\n\n"
    )
    assert "### A?" not in new
    assert "### New?" in new
    assert "## Next" in new

    # Content after the FAQ (a divider then a closing CTA) must be preserved.
    tail = "# H\n\n## FAQ\n\n### A?\n\nx\n\n---\n\n**Build the habit with ExamOS.**\n"
    assert count_faq_questions(tail) == 1
    new_tail = replace_faq_section(
        tail, "## Frequently Asked Questions (FAQ)\n\n### N?\n\ny\n\n"
    )
    assert "**Build the habit with ExamOS.**" in new_tail
    assert "### A?" not in new_tail


def test_generate_correction_regenerates_short_faq():
    body = "# Azure Guide\n\nAzure is a platform.\n\n## FAQ\n\n### Only one?\n\nA\n"
    doc = _doc(body=body)
    seo = {
        "primary_keyword": "Azure",
        "title": "Azure Guide",
        "seoTitle": "Azure Guide",
        "seoDescription": "Azure Guide",
    }
    faq = {
        "faq": [{"question": f"Q{i}?", "answer": "Answer."} for i in range(1, 5)],
        "sources": ["https://learn.microsoft.com/azure"],
    }
    result = generate_correction(
        doc,
        "azure-guide",
        SPEC,
        RoutingLLM(seo=seo),
        search_llm=RoutingLLM(seo=seo, faq=faq),
        verify_faq=True,
        fix_facts=False,
    )
    assert "### Only one?" not in result.new_text
    assert result.new_text.count("### Q") == 4
    assert any("regenerated FAQ" in note for note in result.notes)


def _exam_doc(body):
    desc = "AI-103 " + "detail " * 20
    fm = (
        "slug: how-hard-is-ai-103\n"
        'title: "How Hard Is AI-103 Really?"\n'
        'seoTitle: "How Hard Is the AI-103 Exam"\n'
        f'seoDescription: "{desc}"\n'
    )
    return _doc(slug="how-hard-is-ai-103", frontmatter=fm, body=body)


def _exam_seo():
    return {
        "primary_keyword": "AI-103",
        "title": "How Hard Is AI-103 Really?",
        "seoTitle": "How Hard Is the AI-103 Exam",
        "seoDescription": "AI-103 " + "detail " * 20,
    }


def test_generate_correction_applies_sourced_fact_fix():
    body = "# AI-103 Guide\n\nThe SCS-C02 exam is the current one.\n\n## FAQ\n\n### Q\n\nA\n"
    facts = {
        "issues": [
            {
                "find": "SCS-C02",
                "replace": "SCS-C03",
                "reason": "code was updated",
                "source": "https://aws.amazon.com/certification/",
            }
        ]
    }
    result = generate_correction(
        _exam_doc(body),
        "how-hard-is-ai-103",
        SPEC,
        RoutingLLM(seo=_exam_seo()),
        search_llm=RoutingLLM(seo=_exam_seo(), facts=facts),
        verify_faq=False,
        fix_facts=True,
    )
    assert "SCS-C03" in result.new_text
    assert "SCS-C02" not in result.new_text
    assert any("fixed fact" in note for note in result.notes)
    assert result.sources == ["https://aws.amazon.com/certification/"]


def test_generate_correction_skips_unsourced_fact_fix():
    body = "# AI-103 Guide\n\nThe SCS-C02 exam is the current one.\n"
    facts = {"issues": [{"find": "SCS-C02", "replace": "SCS-C03", "source": ""}]}
    result = generate_correction(
        _exam_doc(body),
        "how-hard-is-ai-103",
        SPEC,
        RoutingLLM(seo=_exam_seo()),
        search_llm=RoutingLLM(seo=_exam_seo(), facts=facts),
        verify_faq=False,
        fix_facts=True,
    )
    assert "SCS-C02" in result.new_text
    assert any("no source" in warning for warning in result.warnings)


def test_apply_replacements_fixes_every_occurrence():
    from contentos.agents.corrector import _apply_replacements

    text = "SCS-C02 appears here and SCS-C02 appears there"
    fixed, applied, sources, warnings = _apply_replacements(
        text,
        [{"find": "SCS-C02", "replace": "SCS-C03", "source": "https://aws.amazon.com"}],
    )
    assert applied == ["SCS-C02 -> SCS-C03 (x2)"]
    assert fixed.count("SCS-C03") == 2
    assert "SCS-C02" not in fixed
    assert warnings == []
    assert sources == ["https://aws.amazon.com"]

    short, _, _, short_warnings = _apply_replacements(
        "in year 27", [{"find": "27", "replace": "28", "source": "https://x.com"}]
    )
    assert short == "in year 27"
    assert any("ambiguous/short" in warning for warning in short_warnings)


def test_apply_replacements_handles_three_char_codes():
    from contentos.agents.corrector import _apply_replacements

    text = "The PMP exam and the PMP handbook, but not PMPXYZ."
    fixed, applied, _, warnings = _apply_replacements(
        text, [{"find": "PMP", "replace": "PMPX", "source": "https://pmi.org"}]
    )
    # Word boundaries mean the 3-char code is fixed but not inside "PMPXYZ".
    assert fixed == "The PMPX exam and the PMPX handbook, but not PMPXYZ."
    assert applied == ["PMP -> PMPX (x2)"]
    assert warnings == []


def test_apply_phrase_replacements_guards_short_and_replaces_repeats():
    from contentos.agents.corrector import _apply_phrase_replacements

    short, _, warnings = _apply_phrase_replacements(
        "Short.", [{"find": "Short.", "replace": "Long.", "reason": "fragment"}]
    )
    assert short == "Short."
    assert any("too short" in warning for warning in warnings)

    repeated = "How to Actually Prepare\n\nHow to Actually Prepare\n"
    out, applied, warnings = _apply_phrase_replacements(
        repeated,
        [{"find": "How to Actually Prepare", "replace": "How to Prepare", "reason": "intensifier"}],
    )
    assert out == "How to Prepare\n\nHow to Prepare\n"
    assert applied == ["How to Actually Prepare -> How to Prepare (x2)"]
    assert warnings == []


def test_generate_correction_applies_deslop():
    body = (
        "# AI-103 Guide\n\n"
        "Take it because you genuinely need the foundation. The word \"correct\" matters.\n\n"
        "## FAQ\n\n### Q\n\nA\n"
    )
    slop = {
        "issues": [
            {
                "find": "Take it because you genuinely need the foundation.",
                "replace": "Take it because you need the foundation.",
                "reason": "empty intensifier",
            },
            {
                "find": 'The word "correct" matters.',
                "replace": "The word correct matters.",
                "reason": "scare quote",
            },
        ]
    }
    result = generate_correction(
        _exam_doc(body),
        "how-hard-is-ai-103",
        SPEC,
        RoutingLLM(seo=_exam_seo(), slop=slop),
        verify_faq=False,
        fix_facts=False,
        fix_grammar=False,
        fix_slop=True,
    )
    assert "genuinely" not in result.new_text
    assert '"correct"' not in result.new_text
    assert any("de-slop:" in note for note in result.notes)


def test_generate_correction_applies_grammar_fix():
    body = (
        "# AI-103 Guide\n\nAzure is a platform. Which requires careful planning.\n\n"
        "## FAQ\n\n### Q\n\nA\n"
    )
    grammar = {
        "issues": [
            {
                "find": "Which requires careful planning.",
                "replace": "It requires careful planning.",
                "reason": "fragment",
            }
        ]
    }
    result = generate_correction(
        _exam_doc(body),
        "how-hard-is-ai-103",
        SPEC,
        RoutingLLM(seo=_exam_seo(), grammar=grammar),
        verify_faq=False,
        fix_facts=False,
        fix_grammar=True,
    )
    assert "It requires careful planning." in result.new_text
    assert "Which requires careful planning." not in result.new_text
    assert any("grammar:" in note for note in result.notes)


def test_fields_needing_rewrite_over_and_keyword_missing():
    keyword = "Azure"
    over_title = "Azure " + "Guide " * 13  # > 71.5 chars
    desc = "Azure " + "detail " * 20
    fm = (
        f'title: "{over_title}"\n'
        'seoTitle: "Azure Certification Guide for Beginners"\n'
        f'seoDescription: "{desc}"\n'
    )
    needs = fields_needing_rewrite(SPEC, fm, keyword)
    assert "title" in needs

    cloud_desc = "Cloud " + "detail " * 19
    missing_kw = (
        'title: "Cloud Guide for Everyone"\n'
        'seoTitle: "Cloud Guide for Everyone Today"\n'
        f'seoDescription: "{cloud_desc}"\n'
    )
    needs = fields_needing_rewrite(SPEC, missing_kw, keyword)
    assert {"title", "seoTitle", "seoDescription"} <= set(needs)

    # Under the minimum but keyword present and not over: no rewrite.
    short = (
        'title: "Azure Certification"\n'
        'seoTitle: "Azure Certification Guide"\n'
        'seoDescription: "Azure practice tests and study plans for certification"\n'
    )
    assert fields_needing_rewrite(SPEC, short, keyword) == []


def test_keyword_in_first_words():
    body = "# Azure Guide\n\n" + " ".join(["word"] * 150)
    assert keyword_in_first_words(body, "Azure")
    late = "# Intro\n\n" + " ".join(["word"] * 150) + " Azure"
    assert not keyword_in_first_words(late, "Azure")


def test_generate_correction_deterministic_survives_empty_seo():
    body = "# Azure Guide\n\nAzure is a platform.\n\n## Fast Answers\n\n### Q\n\nA\n"
    doc = _doc(body=body)
    result = generate_correction(
        doc, "azure-guide", SPEC, RoutingLLM(seo={}), verify_faq=False, seo=True
    )
    assert FAQ_HEADING in result.new_text
    assert "Fast Answers" not in result.new_text
    assert any("FAQ heading" in note for note in result.notes)


def test_generate_correction_skips_seo_by_default():
    title = "Azure " + "Guide " * 13
    fm = (
        'slug: azure-guide\n'
        f'title: "{title}"\n'
        'seoTitle: "Azure Certification Guide for Beginners"\n'
        'seoDescription: "' + ("Azure " + "detail " * 20) + '"\n'
    )
    doc = _doc(frontmatter=fm, body="# Azure Guide\n\nAzure is a platform.\n")
    seo = {
        "primary_keyword": "Azure",
        "title": "Azure Certification Guide for Beginners and Beyond 2026",
        "seoTitle": "Azure Certification Guide for Beginners",
        "seoDescription": "Azure " + "detail " * 20,
    }
    result = generate_correction(doc, "azure-guide", SPEC, RoutingLLM(seo=seo), verify_faq=False)
    assert get_field(result.new_text.split("---")[1], "title") == title
    assert not any("title:" in note for note in result.notes)


def test_generate_correction_applies_valid_seo():
    keyword = "Azure"
    title = "Azure " + "Guide " * 13
    fm = (
        'slug: azure-guide\n'
        f'title: "{title}"\n'
        'seoTitle: "Azure Certification Guide for Beginners"\n'
        'seoDescription: "' + ("Azure " + "detail " * 20) + '"\n'
    )
    body = "# Azure Guide\n\nAzure is a platform.\n\n## FAQ\n\n### Q\n\nA\n"
    doc = _doc(frontmatter=fm, body=body)
    seo = {
        "primary_keyword": keyword,
        "title": "Azure Certification Guide for Beginners and Beyond 2026",
        "seoTitle": "Azure Certification Guide for Beginners",
        "seoDescription": "Azure " + "detail " * 20,
    }
    result = generate_correction(
        doc, "azure-guide", SPEC, RoutingLLM(seo=seo), verify_faq=False, seo=True
    )
    assert get_field(result.new_text.split("---")[1], "title") == seo["title"]
    assert any("title:" in note for note in result.notes)


def test_generate_correction_rejects_invalid_seo_candidate():
    keyword = "Azure"
    title = "Azure " + "Guide " * 13
    desc = "Azure " + "detail " * 20
    fm = (
        f'title: "{title}"\n'
        'seoTitle: "Azure Certification Guide for Beginners"\n'
        f'seoDescription: "{desc}"\n'
    )
    doc = _doc(frontmatter=fm, body="# Azure Guide\n\nAzure.\n")
    seo = {"primary_keyword": keyword, "title": "No keyword here at all"}
    result = generate_correction(
        doc, "azure-guide", SPEC, RoutingLLM(seo=seo), verify_faq=False, seo=True
    )
    assert any("rejected" in warning for warning in result.warnings)


def test_generate_correction_adds_faq_with_search():
    body = "# Azure Guide\n\nAzure is a platform.\n"
    doc = _doc(body=body)
    seo = {
        "primary_keyword": "Azure",
        "title": "Azure Guide",
        "seoTitle": "Azure Guide",
        "seoDescription": "Azure Guide",
    }
    faq = {
        "faq": [
            {"question": f"Question {i}?", "answer": "Answer text."} for i in range(1, 5)
        ],
        "sources": ["https://learn.microsoft.com/azure"],
    }
    llm = RoutingLLM(seo=seo)
    search_llm = RoutingLLM(seo=seo, faq=faq)
    result = generate_correction(
        doc, "azure-guide", SPEC, llm, search_llm=search_llm, verify_faq=True
    )
    assert has_faq_heading(result.new_text)
    assert result.new_text.count("### Question") == 4
    assert result.sources == ["https://learn.microsoft.com/azure"]


def test_generate_correction_warns_when_faq_unverified():
    doc = _doc(body="# Azure Guide\n\nAzure is a platform.\n")
    result = generate_correction(doc, "azure-guide", SPEC, RoutingLLM(seo={}), verify_faq=False)
    assert any("FAQ missing" in warning for warning in result.warnings)
    assert not has_faq_heading(result.new_text)


def _write_blog(tmp_path, slug, body):
    blog = tmp_path / "blog"
    blog.mkdir(exist_ok=True)
    (blog / f"{slug}.mdx").write_text(
        f'---\nslug: {slug}\ntitle: "Post"\nseoTitle: "Post"\n'
        f'seoDescription: "Post"\n---\n{body}',
        encoding="utf-8",
    )
    return blog


def test_run_correct_blogs_dry_run_and_apply(tmp_path, monkeypatch):
    blog = _write_blog(tmp_path, "post", "# Post\n\nBody.\n\n## Fast Answers\n\n### Q\n\nA\n")
    monkeypatch.setattr(pipeline, "gemini_llm", lambda **_kwargs: RoutingLLM(seo={}))

    report = pipeline.run_correct_blogs(
        input_dir=str(blog),
        output_dir=str(tmp_path / "out"),
        reports_dir=str(tmp_path / "reports"),
        prompt_path=str(PROMPT),
        workers=1,
        verify_faq=False,
    )
    assert report.files_scanned == 1
    assert report.files_changed == 1
    assert Path(report.extra["diff_report"]).read_text(encoding="utf-8")
    assert not (tmp_path / "out").exists()

    payload = json.loads(Path(report.extra["json_report"]).read_text(encoding="utf-8"))
    assert payload["files_changed"] == 1

    applied = pipeline.run_correct_blogs(
        input_dir=str(blog),
        output_dir=str(tmp_path / "out"),
        reports_dir=str(tmp_path / "reports"),
        prompt_path=str(PROMPT),
        apply=True,
        workers=1,
        verify_faq=False,
    )
    assert applied.files_changed == 1
    corrected = (tmp_path / "out" / "post.mdx").read_text(encoding="utf-8")
    assert FAQ_HEADING in corrected
    assert "Fast Answers" not in corrected


def test_load_rating_scores_valid_and_malformed(tmp_path):
    valid = tmp_path / "valid.json"
    valid.write_text(
        json.dumps(
            {
                "ratings": [
                    {"slug": "a", "scores": {"voice_ai_slop": 2.0, "seo_quality": 3.0}},
                    {"slug": "b", "scores": {"voice_ai_slop": 5.0}},
                ]
            }
        ),
        encoding="utf-8",
    )
    scores = reporting.load_rating_scores(str(valid))
    assert scores["a"]["voice_ai_slop"] == 2.0
    assert scores["b"]["voice_ai_slop"] == 5.0

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text(
        '{"ratings":[{"slug":"a","scores":{"voice_ai_slop":1.0}}\n'
        '{"slug":"b","scores":{"voice_ai_slop":4.0}}]}',
        encoding="utf-8",
    )
    fallback = reporting.load_rating_scores(str(corrupt))
    assert fallback["a"]["voice_ai_slop"] == 1.0
    assert fallback["b"]["voice_ai_slop"] == 4.0


def test_load_rating_grades_valid_and_malformed(tmp_path):
    valid = tmp_path / "valid.json"
    valid.write_text(
        json.dumps(
            {
                "ratings": [
                    {"slug": "a", "average": 3.8, "grade": "B+"},
                    {"slug": "b", "average": 4.5, "grade": "A+"},
                ]
            }
        ),
        encoding="utf-8",
    )
    grades = reporting.load_rating_grades(str(valid))
    assert grades["a"] == {"average": 3.8, "grade": "B+"}
    assert grades["b"]["average"] == 4.5

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text(
        '{"ratings":[{"slug":"a","average":3.1,"grade":"B-"}\n'
        '{"slug":"b","average":4.4,"grade":"A"}]}',
        encoding="utf-8",
    )
    fallback = reporting.load_rating_grades(str(corrupt))
    assert fallback["a"]["average"] == 3.1
    assert fallback["b"]["grade"] == "A"


def test_phrase_fix_rejects_length_blowup():
    from contentos.agents.corrector import _apply_phrase_replacements

    find = "This sentence is short and fine."
    replace = "This sentence " + "and more words " * 20
    out, applied, warnings = _apply_phrase_replacements(
        find, [{"find": find, "replace": replace}], SLOP_MAX_GROWTH, SLOP_MIN_SHRINK
    )
    assert out == find
    assert applied == []
    assert any("length changed too much" in warning for warning in warnings)


def test_run_correct_blogs_deslop_gate(tmp_path, monkeypatch):
    body = (
        "# Post\n\nTake it because you genuinely need the foundation.\n\n"
        "## FAQ\n\n### Q\n\nA\n"
    )
    blog = _write_blog(tmp_path, "post", body)
    slop = {
        "issues": [
            {
                "find": "Take it because you genuinely need the foundation.",
                "replace": "Take it because you need the foundation.",
                "reason": "empty intensifier",
            }
        ]
    }
    monkeypatch.setattr(
        pipeline, "gemini_llm", lambda **_kwargs: RoutingLLM(seo={}, slop=slop)
    )

    skipped = pipeline.run_correct_blogs(
        input_dir=str(blog),
        reports_dir=str(tmp_path / "reports-a"),
        prompt_path=str(PROMPT),
        workers=1,
        verify_faq=False,
        fix_facts=False,
        fix_grammar=False,
        deslop_slugs=set(),
    )
    assert not any("de-slop" in note for note in skipped.changes[0].notes)

    applied = pipeline.run_correct_blogs(
        input_dir=str(blog),
        reports_dir=str(tmp_path / "reports-b"),
        prompt_path=str(PROMPT),
        workers=1,
        verify_faq=False,
        fix_facts=False,
        fix_grammar=False,
        deslop_slugs={"post"},
    )
    assert any("de-slop" in note for note in applied.changes[0].notes)


def test_run_correct_blogs_slug_filter(tmp_path, monkeypatch):
    blog = tmp_path / "blog"
    blog.mkdir()
    for name in ("keep-me", "skip-me"):
        (blog / f"{name}.mdx").write_text(
            f'---\nslug: {name}\ntitle: "P"\n---\n# {name}\n\nBody.\n', encoding="utf-8"
        )
    monkeypatch.setattr(pipeline, "gemini_llm", lambda **_kwargs: RoutingLLM(seo={}))

    report = pipeline.run_correct_blogs(
        input_dir=str(blog),
        reports_dir=str(tmp_path / "reports"),
        prompt_path=str(PROMPT),
        workers=1,
        verify_faq=False,
        fix_facts=False,
        fix_grammar=False,
        fix_slop=False,
        slug_filter={"keep-me"},
    )
    assert [change.slug for change in report.changes] == ["keep-me"]


RATING_PROMPT = Path(__file__).resolve().parents[1] / "prompts" / "examos-blog-rating.md"
RATING_RUBRIC = load_rubric(str(RATING_PROMPT))
FULL_SCORES = {criterion.key: 5 for criterion in RATING_RUBRIC.criteria}
LOW_SCORES = {criterion.key: 1 for criterion in RATING_RUBRIC.criteria}


def _rating_payload(scores):
    return {"scores": scores, "factual_issues": [], "strengths": [], "weaknesses": [], "summary": "x"}


def test_parse_document_roundtrip():
    from contentos.core.loaders import parse_document

    doc = parse_document("---\nslug: x\n---\nBody\n", "x.mdx")
    assert doc.has_frontmatter and doc.frontmatter == "slug: x" and doc.body == "Body\n"


def test_generate_correction_fixes_invented_examos_feature():
    body = (
        "# AI-103 Guide\n\nUse ExamOS Mentor Mode to track your progress.\n\n"
        "## FAQ\n\n### Q\n\nA\n"
    )
    examos = {
        "issues": [
            {
                "find": "Use ExamOS Mentor Mode to track your progress.",
                "replace": "Use ExamOS Daily Sprint to track your progress.",
                "reason": "invented feature",
            }
        ]
    }
    result = generate_correction(
        _exam_doc(body),
        "how-hard-is-ai-103",
        SPEC,
        RoutingLLM(seo=_exam_seo(), examos=examos),
        verify_faq=False,
        fix_facts=False,
        fix_grammar=False,
        fix_slop=False,
        fix_examos=True,
    )
    assert "Mentor Mode" not in result.new_text
    assert "Daily Sprint" in result.new_text
    assert any("ExamOS:" in note for note in result.notes)


def test_run_correct_blogs_review_flags_below_threshold(tmp_path, monkeypatch):
    blog = _write_blog(tmp_path, "post", "# Post\n\nBody.\n\n## Fast Answers\n\n### Q\n\nA\n")
    monkeypatch.setattr(
        pipeline, "gemini_llm", lambda **_kwargs: RoutingLLM(seo={}, rating=_rating_payload(LOW_SCORES))
    )
    report = pipeline.run_correct_blogs(
        input_dir=str(blog),
        reports_dir=str(tmp_path / "reports"),
        prompt_path=str(PROMPT),
        workers=1,
        verify_faq=False,
        fix_facts=False,
        fix_grammar=False,
        fix_slop=False,
        fix_examos=False,
        review=True,
    )
    assert report.changes[0].review_flagged is True
    assert report.files_flagged == 1
    # A separate post-correction rating report + a human-review file are written.
    assert Path(report.extra["rating_report"]).exists()
    rating = json.loads(Path(report.extra["rating_json"]).read_text(encoding="utf-8"))
    assert rating["files_rated"] == 1
    human = Path(report.extra["human_review_report"])
    assert human.exists()
    text = human.read_text(encoding="utf-8")
    assert "Issues to fix" in text and "post" in text


def test_run_correct_blogs_review_passes_when_high(tmp_path, monkeypatch):
    blog = _write_blog(tmp_path, "post", "# Post\n\nBody.\n\n## Fast Answers\n\n### Q\n\nA\n")
    monkeypatch.setattr(
        pipeline, "gemini_llm", lambda **_kwargs: RoutingLLM(seo={}, rating=_rating_payload(FULL_SCORES))
    )
    report = pipeline.run_correct_blogs(
        input_dir=str(blog),
        reports_dir=str(tmp_path / "reports"),
        prompt_path=str(PROMPT),
        workers=1,
        verify_faq=False,
        fix_facts=False,
        fix_grammar=False,
        fix_slop=False,
        fix_examos=False,
        review=True,
    )
    assert report.changes[0].review_flagged is False
    assert report.files_flagged == 0
    assert report.changes[0].review_grade == "A+"
    assert Path(report.extra["rating_report"]).exists()
    assert "human_review_report" not in report.extra


def test_faq_applies_false_never_adds_a_section():
    # Roadmaps / exam-faq set faq.applies=false in their profile.
    body = "# AI-103 Guide\n\nAzure is a platform.\n"
    faq = {"faq": [{"question": "Q", "answer": "A"}]}
    result = generate_correction(
        _exam_doc(body),
        "how-hard-is-ai-103",
        SPEC,
        RoutingLLM(seo=_exam_seo(), faq=faq),
        search_llm=RoutingLLM(seo=_exam_seo(), faq=faq),
        verify_faq=True,
        fix_facts=False,
        fix_grammar=False,
        fix_slop=False,
        fix_examos=False,
        faq_applies=False,
    )
    assert FAQ_HEADING not in result.new_text
    assert not any("FAQ" in note for note in result.notes)


def test_code_fence_content_is_never_rewritten():
    body = (
        "# H\n\n```python\nvalue = 'really nice'\n```\n\n"
        "This sentence has really nice prose.\n"
    )
    slop = {
        "issues": [
            {"find": "value = 'really nice'", "replace": "value = 'nice'", "reason": "slop"},
            {
                "find": "This sentence has really nice prose.",
                "replace": "This sentence has nice prose.",
                "reason": "slop",
            },
        ]
    }
    result = generate_correction(
        _doc(body=body),
        "post",
        SPEC,
        RoutingLLM(seo={}, slop=slop),
        verify_faq=False,
        fix_facts=False,
        fix_grammar=False,
        fix_slop=True,
        fix_examos=False,
    )
    assert "value = 'really nice'" in result.new_text
    assert "value = 'nice'" not in result.new_text
    assert "This sentence has nice prose." in result.new_text


def test_phrase_fix_that_adds_a_link_is_rejected():
    body = "# H\n\nReady? (/exams/foo) offers practice today.\n"
    grammar = {
        "issues": [
            {
                "find": "(/exams/foo) offers practice today.",
                "replace": "[ExamOS](/exams/foo) offers practice today.",
                "reason": "fragment",
            }
        ]
    }
    result = generate_correction(
        _doc(body=body),
        "post",
        SPEC,
        RoutingLLM(seo={}, grammar=grammar),
        verify_faq=False,
        fix_facts=False,
        fix_grammar=True,
        fix_slop=False,
        fix_examos=False,
    )
    assert "](" not in result.new_text
    assert any("would add a link" in warning for warning in result.warnings)
