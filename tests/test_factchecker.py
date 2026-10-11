"""Fact checker agent tests (no network)."""

from __future__ import annotations

import json
from pathlib import Path

from contentos.agents.factchecker import (
    FactCheckResult,
    FactRegistry,
    _apply_llm_issues,
    generate_factcheck,
    link_exams,
    load_fact_registry,
)
from contentos.core import pipeline
from contentos.core.loaders import parse_document

FACT = {
    "updated_at": "2026-01-01",
    "exams": [
        {
            "slug": "test-exam-xx-100",
            "code": "XX-100",
            "official_name": "Test Exam XX-100",
            "other_names": ["Test Certification"],
            "vendor": "Vendor",
            "status": "active",
            "replaces": "XX-099",
            "replaced_by": None,
            "predecessor_last_date": {
                "value_type": "exact",
                "value": "2026-01-31",
                "source_url": "https://ex.com",
                "note": None,
            },
            "ga_month_year": {"value_type": "exact", "value": "2025-01", "source_url": "https://ex.com", "note": None},
            "ga_date_exact": {"value_type": "exact", "value": "2025-01-15", "source_url": "https://ex.com", "note": None},
            "last_exam_update_month": {"value_type": "exact", "value": "2025-01", "source_url": "https://ex.com", "note": None},
            "last_day_to_take": {"value_type": "unpublished", "value": None, "source_url": "https://ex.com", "note": None},
            "passing_score": {"value_type": "exact", "value": 700, "source_url": "https://ex.com", "note": None},
            "price_usd": {"value_type": "exact", "value": 200, "source_url": "https://ex.com", "note": None},
            "duration_minutes": {"value_type": "exact", "value": 120, "source_url": "https://ex.com", "note": None},
            "scored_questions": {"value_type": "exact", "value": 50, "source_url": "https://ex.com", "note": None},
            "unscored_questions": {"value_type": "unpublished", "value": None, "source_url": "https://ex.com", "note": None},
            "domains": [],
            "allowed_services": ["New Service"],
            "retired_or_retiring_services": [
                {
                    "name": "Old Service",
                    "status": "retired",
                    "retirement_date": "2025-01-01",
                    "successor": "New Service",
                    "source_url": "https://ex.com",
                }
            ],
            "out_of_scope_for_exam": [
                {"name": "Out Service", "reason": "not tested", "source_url": "https://ex.com"}
            ],
            "renames": [{"from": "Old Name", "to": "New Name"}],
            "sources": [
                {
                    "url": "https://ex.com",
                    "official": True,
                    "covers": ["price_usd", "passing_score", "renames", "status", "scored_questions"],
                }
            ],
            "last_verified": "2026-01-01",
        }
    ],
}

FM = (
    "slug: post\n"
    'title: "Test"\n'
    'seoTitle: "Test"\n'
    'seoDescription: "Test"\n'
    "type: post\n"
    "imagePosition: top\n"
    'author: "A"\n'
    'categories: ["ai"]\n'
    'date: "2026-01-01"\n'
    'examCodes: ["XX-100"]'
)


def _registry(tmp_path) -> FactRegistry:
    facts_dir = tmp_path / "facts"
    facts_dir.mkdir()
    (facts_dir / "test-exam-xx-100.json").write_text(json.dumps(FACT), encoding="utf-8")
    return FactRegistry(load_fact_registry(str(facts_dir)))


def _doc(slug, frontmatter, body):
    return parse_document(f"---\n{frontmatter}\n---\n{body}", f"{slug}.mdx")


def test_load_registry(tmp_path):
    registry = _registry(tmp_path)
    assert len(registry.facts) == 1
    assert registry.for_code("xx-100").official_name == "Test Exam XX-100"


def test_link_exams_by_frontmatter_is_primary(tmp_path):
    registry = _registry(tmp_path)
    doc = _doc("post", FM, "# H\n\nBody.\n")
    exams, primary = link_exams(doc, "post", registry)
    assert [fact.code for fact in exams] == ["XX-100"]
    assert primary == {"XX-100"}


def test_link_exams_body_only_is_secondary(tmp_path):
    registry = _registry(tmp_path)
    doc = _doc("post", "slug: post", "# H\n\nThe XX-100 exam is hard.\n")
    exams, primary = link_exams(doc, "post", registry)
    assert [fact.code for fact in exams] == ["XX-100"]
    assert primary == set()


def test_rename_applied_and_blocked_in_discussion(tmp_path):
    registry = _registry(tmp_path)
    doc = _doc("post", FM, "# H\n\nLearn Old Name for the exam.\n")
    result = generate_factcheck(doc, "post", registry)
    assert "New Name" in result.new_text
    assert "Old Name" not in result.new_text

    discussed = _doc("post", FM, "# H\n\nOld Name was formerly the product name.\n")
    blocked = generate_factcheck(discussed, "post", registry)
    assert "Old Name" in blocked.new_text
    assert any("rename discussion" in finding["reason"] for finding in blocked.findings)


def test_body_only_exam_does_not_autofix(tmp_path):
    registry = _registry(tmp_path)
    doc = _doc("post", "slug: post", "# H\n\nThe XX-100 exam uses Old Name.\n")
    result = generate_factcheck(doc, "post", registry)
    assert "Old Name" in result.new_text
    assert any(not finding["applied"] for finding in result.findings)


def test_question_and_predecessor_date_fixes(tmp_path):
    registry = _registry(tmp_path)
    body = "# H\n\nThe XX-100 exam has 60 questions. XX-099 retired on February 1, 2026.\n"
    result = generate_factcheck(_doc("post", FM, body), "post", registry)
    assert "50 questions" in result.new_text
    assert "January 31, 2026" in result.new_text


def test_salary_number_is_not_an_exam_fee(tmp_path):
    registry = _registry(tmp_path)
    result = generate_factcheck(
        _doc("post", FM, "# H\n\nAverage pay is $90,000 for these roles.\n"), "post", registry
    )
    assert "$90,000" in result.new_text
    assert not any(finding["category"] == "price" for finding in result.findings)


def test_retired_and_out_of_scope_services_are_flagged(tmp_path):
    registry = _registry(tmp_path)
    body = "# H\n\nThe XX-100 exam covers Old Service and Out Service.\n"
    result = generate_factcheck(_doc("post", FM, body), "post", registry)
    categories = {finding["category"] for finding in result.findings}
    assert "retired_service" in categories
    assert "out_of_scope" in categories
    assert result.new_text == _doc("post", FM, body).render()  # nothing auto-applied


def test_body_and_frontmatter_preserved_on_apply(tmp_path):
    registry = _registry(tmp_path)
    doc = _doc("post", FM, "# H\n\nLearn Old Name.\n")
    result = generate_factcheck(doc, "post", registry)
    assert result.new_text.startswith("---\nexamCodes:") or 'slug: post' in result.new_text
    assert "# H\n\nLearn New Name.\n" in result.new_text


def test_apply_llm_issues():
    result = FactCheckResult(slug="x", filename="x.mdx", new_text="")
    body = _apply_llm_issues(
        "The fee is $300.",
        {
            "issues": [
                {"find": "$300", "replace": "$200", "reason": "wrong fee", "source": "https://ex.com"}
            ],
            "unresolved": ["confirm the date"],
        },
        result,
    )
    assert body == "The fee is $200."
    assert result.sources == ["https://ex.com"]
    assert any("confirm the date" in item for item in result.review_items)


DOMAIN_FACT = {
    "updated_at": "2026-01-01",
    "exams": [
        {
            "slug": "azure-exam-az-999",
            "code": "AZ-999",
            "official_name": "Azure Test Exam",
            "other_names": [],
            "vendor": "Microsoft",
            "status": "active",
            "replaces": None,
            "replaced_by": None,
            "predecessor_last_date": {"value_type": "unpublished", "value": None},
            "ga_month_year": {"value_type": "unpublished", "value": None},
            "ga_date_exact": {"value_type": "unpublished", "value": None},
            "last_exam_update_month": {"value_type": "unpublished", "value": None},
            "last_day_to_take": {"value_type": "unpublished", "value": None},
            "passing_score": {"value_type": "exact", "value": 700, "source_url": "https://ex.com"},
            "price_usd": {"value_type": "exact", "value": 165, "source_url": "https://ex.com"},
            "duration_minutes": {"value_type": "exact", "value": 100, "source_url": "https://ex.com"},
            "scored_questions": {"value_type": "exact", "value": 50, "source_url": "https://ex.com"},
            "unscored_questions": {"value_type": "unpublished", "value": None},
            "domains": [
                {"name": "Alpha Solutions", "weight_percent": "30-40"},
                {"name": "Beta Operations", "weight_percent": "60-70"},
            ],
            "allowed_services": ["Azure Monitor"],
            "retired_or_retiring_services": [],
            "out_of_scope_for_exam": [],
            "renames": [],
            "sources": [
                {"url": "https://ex.com", "official": True, "covers": ["domains", "price_usd"]}
            ],
            "last_verified": "2026-01-01",
        }
    ],
}

DOM_FM = (
    "slug: post\n"
    'title: "Test"\n'
    'type: post\n'
    'imagePosition: top\n'
    'examCodes: ["AZ-999"]'
)


def _domain_registry(tmp_path) -> FactRegistry:
    facts_dir = tmp_path / "facts2"
    facts_dir.mkdir()
    (facts_dir / "azure-exam-az-999.json").write_text(json.dumps(DOMAIN_FACT), encoding="utf-8")
    return FactRegistry(load_fact_registry(str(facts_dir)))


def test_domain_weight_mismatch_flagged(tmp_path):
    registry = _domain_registry(tmp_path)
    body = "# H\n\nThe Alpha Solutions domain is only 10-20% of the exam.\n"
    result = generate_factcheck(_doc("post", DOM_FM, body), "post", registry)
    assert any(finding["category"] == "domain_weight" for finding in result.findings)


def test_grouped_domain_math_flagged(tmp_path):
    registry = _domain_registry(tmp_path)
    body = "# H\n\nThe two domains represent 5-10% of the exam.\n"
    result = generate_factcheck(_doc("post", DOM_FM, body), "post", registry)
    assert any(finding["category"] == "domain_weight_math" for finding in result.findings)


def test_domain_weight_reads_its_own_table_row(tmp_path):
    # The row above sits inside the scan window; each domain must read its own weight,
    # not the preceding row's (AZ-104 "Monitor and maintain Azure resources" false positive).
    registry = _domain_registry(tmp_path)
    body = (
        "# H\n\n"
        "| Domain | Weight |\n|--------|--------|\n"
        "| Alpha Solutions | 30-40% |\n"
        "| Beta Operations | 60-70% |\n"
    )
    result = generate_factcheck(_doc("post", DOM_FM, body), "post", registry)
    assert not any(finding["category"] == "domain_weight" for finding in result.findings)


def test_vendor_contamination_flagged_and_comparison_ignored(tmp_path):
    registry = _domain_registry(tmp_path)
    contamination = "# H\n\nUse Amazon CloudWatch to monitor the workload.\n"
    flagged = generate_factcheck(_doc("post", DOM_FM, contamination), "post", registry)
    assert any(finding["category"] == "vendor_contamination" for finding in flagged.findings)

    comparison = "# H\n\nUnlike AWS CloudWatch, Azure Monitor is the native option.\n"
    compared = generate_factcheck(_doc("post", DOM_FM, comparison), "post", registry)
    assert not any(finding["category"] == "vendor_contamination" for finding in compared.findings)


def test_exam_slug_in_url_is_not_a_mention(tmp_path):
    registry = _domain_registry(tmp_path)
    body = "# H\n\nSee [the comparison](/blog/azure-exam-az-999-vs-other).\n"
    exams, _primary = link_exams(_doc("post", "slug: post", body), "post", registry)
    assert exams == []


def test_comparison_does_not_misattribute_a_value(tmp_path):
    # The document mentions two exams; a claim near one must not be applied to the other.
    facts_dir = tmp_path / "facts3"
    facts_dir.mkdir()
    (facts_dir / "a.json").write_text(
        json.dumps(
            {
                "exams": [
                    {
                        "slug": "exam-a-aa-1",
                        "code": "AA-1",
                        "official_name": "Alpha Exam",
                        "other_names": [],
                        "vendor": "Microsoft",
                        "status": "active",
                        "replaces": None,
                        "replaced_by": None,
                        "predecessor_last_date": {"value_type": "unpublished", "value": None},
                        "ga_month_year": {"value_type": "unpublished", "value": None},
                        "ga_date_exact": {"value_type": "unpublished", "value": None},
                        "last_exam_update_month": {"value_type": "unpublished", "value": None},
                        "last_day_to_take": {"value_type": "unpublished", "value": None},
                        "passing_score": {"value_type": "unpublished", "value": None},
                        "price_usd": {"value_type": "unpublished", "value": None},
                        "duration_minutes": {"value_type": "exact", "value": 100, "source_url": "https://ex.com"},
                        "scored_questions": {"value_type": "exact", "value": 50, "source_url": "https://ex.com"},
                        "unscored_questions": {"value_type": "unpublished", "value": None},
                        "domains": [],
                        "allowed_services": [],
                        "retired_or_retiring_services": [],
                        "out_of_scope_for_exam": [],
                        "renames": [],
                        "sources": [{"url": "https://ex.com", "official": True, "covers": ["duration_minutes"]}],
                    },
                    {
                        "slug": "exam-b-bb-2",
                        "code": "BB-2",
                        "official_name": "Beta Exam",
                        "other_names": [],
                        "vendor": "Amazon",
                        "status": "active",
                        "replaces": None,
                        "replaced_by": None,
                        "predecessor_last_date": {"value_type": "unpublished", "value": None},
                        "ga_month_year": {"value_type": "unpublished", "value": None},
                        "ga_date_exact": {"value_type": "unpublished", "value": None},
                        "last_exam_update_month": {"value_type": "unpublished", "value": None},
                        "last_day_to_take": {"value_type": "unpublished", "value": None},
                        "passing_score": {"value_type": "unpublished", "value": None},
                        "price_usd": {"value_type": "unpublished", "value": None},
                        "duration_minutes": {"value_type": "exact", "value": 240, "source_url": "https://ex.com"},
                        "scored_questions": {"value_type": "exact", "value": 75, "source_url": "https://ex.com"},
                        "unscored_questions": {"value_type": "unpublished", "value": None},
                        "domains": [],
                        "allowed_services": [],
                        "retired_or_retiring_services": [],
                        "out_of_scope_for_exam": [],
                        "renames": [],
                        "sources": [{"url": "https://ex.com", "official": True, "covers": ["duration_minutes"]}],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    registry = FactRegistry(load_fact_registry(str(facts_dir)))
    fm = (
        "slug: alpha-exam-aa-1\n"
        "title: Alpha Exam AA-1\n"
        "type: post\n"
        "imagePosition: top\n"
        'examCodes: ["AA-1"]'
    )
    body = "# H\n\nBeta Exam BB-2 has 60 questions in 180 minutes.\n"
    result = generate_factcheck(_doc("post", fm, body), "post", registry)
    assert "60 questions" in result.new_text
    assert "180 minutes" in result.new_text


def test_human_review_report_lists_findings(tmp_path):
    blog = tmp_path / "blog"
    blog.mkdir()
    (blog / "post.mdx").write_text(
        f"---\n{DOM_FM}\n---\n# H\n\nUse Amazon CloudWatch here.\n", encoding="utf-8"
    )
    facts_dir = tmp_path / "facts2"
    facts_dir.mkdir()
    (facts_dir / "azure-exam-az-999.json").write_text(json.dumps(DOMAIN_FACT), encoding="utf-8")
    report = pipeline.run_check_facts(
        input_dir=str(blog),
        reports_dir=str(tmp_path / "reports"),
        facts_dir=str(facts_dir),
        workers=1,
    )
    text = Path(report.extra["markdown_report"]).read_text(encoding="utf-8")
    assert "Needs human review" in text
    assert "vendor_contamination" in text


def test_run_check_facts_dry_run_and_apply(tmp_path):
    blog = tmp_path / "blog"
    blog.mkdir()
    (blog / "post.mdx").write_text(
        f"---\n{FM}\n---\n# H\n\nLearn Old Name for the exam.\n", encoding="utf-8"
    )
    facts_dir = tmp_path / "facts"
    facts_dir.mkdir()
    (facts_dir / "test-exam-xx-100.json").write_text(json.dumps(FACT), encoding="utf-8")

    dry = pipeline.run_check_facts(
        input_dir=str(blog),
        output_dir=str(tmp_path / "out"),
        reports_dir=str(tmp_path / "reports"),
        facts_dir=str(facts_dir),
        workers=1,
    )
    assert dry.files_scanned == 1
    assert dry.files_with_exam == 1
    assert dry.files_changed == 1
    assert not (tmp_path / "out").exists()
    assert Path(dry.extra["json_report"]).exists()
    assert Path(dry.extra["markdown_report"]).exists()

    applied = pipeline.run_check_facts(
        input_dir=str(blog),
        output_dir=str(tmp_path / "out"),
        reports_dir=str(tmp_path / "reports"),
        facts_dir=str(facts_dir),
        apply=True,
        workers=1,
    )
    assert applied.files_changed == 1
    corrected = (tmp_path / "out" / "post.mdx").read_text(encoding="utf-8")
    assert "New Name" in corrected
    assert "Old Name" not in corrected


def test_link_exams_examcode_singular_binding(tmp_path):
    registry = _registry(tmp_path)
    doc = _doc("post", 'slug: post\nexamCode: "XX-100"\ntitle: "T"', "# H\n\nBody.\n")
    exams, primary = link_exams(doc, "post", registry, exam_binding=("examCode",))
    assert [fact.code for fact in exams] == ["XX-100"]
    assert primary == {"XX-100"}


def test_link_exams_filename_slug_binding(tmp_path):
    registry = _registry(tmp_path)
    doc = _doc("test-exam-xx-100", "slug: test-exam-xx-100\ntitle: T", "# H\n\nBody.\n")
    exams, primary = link_exams(
        doc, "test-exam-xx-100", registry, exam_binding=("filename_slug",)
    )
    assert [fact.code for fact in exams] == ["XX-100"]
    assert primary == {"XX-100"}


def test_rename_skips_code_fence(tmp_path):
    registry = _registry(tmp_path)
    body = "# H\n\n```\nOld Name\n```\n\nLearn Old Name today.\n"
    result = generate_factcheck(_doc("post", FM, body), "post", registry)
    assert "```\nOld Name\n```" in result.new_text
    assert "Learn New Name today." in result.new_text


def test_vendor_contamination_can_be_disabled(tmp_path, monkeypatch):
    registry = _registry(tmp_path)
    calls = {"n": 0}
    monkeypatch.setattr(
        "contentos.agents.factchecker._check_vendor_contamination",
        lambda *args, **kwargs: calls.__setitem__("n", calls["n"] + 1),
    )
    doc = _doc("post", FM, "# H\n\nBody with Amazon S3.\n")
    generate_factcheck(doc, "post", registry, vendor_contamination=False)
    assert calls["n"] == 0
    generate_factcheck(doc, "post", registry, vendor_contamination=True)
    assert calls["n"] >= 1


# --- Exam binding: the frontmatter code is authoritative ----------------------


def _two_exam_registry(tmp_path) -> FactRegistry:
    """XX-100 plus a decoy whose stripped name alias is the generic "developer associate"."""
    facts_dir = tmp_path / "facts_bind"
    facts_dir.mkdir()
    payload = {
        "exams": [
            {
                "slug": "test-exam-xx-100",
                "code": "XX-100",
                "official_name": "Test Exam XX-100",
                "other_names": [],
                "vendor": "Vendor",
                "status": "active",
                "replaces": None,
                "replaced_by": None,
                "predecessor_last_date": {"value_type": "unpublished", "value": None},
                "ga_month_year": {"value_type": "unpublished", "value": None},
                "ga_date_exact": {"value_type": "unpublished", "value": None},
                "last_exam_update_month": {"value_type": "unpublished", "value": None},
                "last_day_to_take": {"value_type": "unpublished", "value": None},
                "passing_score": {"value_type": "unpublished", "value": None},
                "price_usd": {"value_type": "exact", "value": 200, "source_url": "https://ex.com"},
                "duration_minutes": {"value_type": "exact", "value": 120, "source_url": "https://ex.com"},
                "scored_questions": {"value_type": "exact", "value": 50, "source_url": "https://ex.com"},
                "unscored_questions": {"value_type": "unpublished", "value": None},
                "domains": [],
                "allowed_services": [],
                "retired_or_retiring_services": [],
                "out_of_scope_for_exam": [],
                "renames": [],
                "sources": [
                    {
                        "url": "https://ex.com",
                        "official": True,
                        "covers": ["price_usd", "duration_minutes", "scored_questions"],
                    }
                ],
            },
            {
                "slug": "aws-developer-associate-dva-c02",
                "code": "DVA-C02",
                "official_name": "AWS Certified Developer - Associate",
                "other_names": [],
                "vendor": "Amazon",
                "status": "active",
                "replaces": None,
                "replaced_by": None,
                "predecessor_last_date": {"value_type": "unpublished", "value": None},
                "ga_month_year": {"value_type": "unpublished", "value": None},
                "ga_date_exact": {"value_type": "unpublished", "value": None},
                "last_exam_update_month": {"value_type": "unpublished", "value": None},
                "last_day_to_take": {"value_type": "unpublished", "value": None},
                "passing_score": {"value_type": "unpublished", "value": None},
                "price_usd": {"value_type": "exact", "value": 150, "source_url": "https://ex.com"},
                "duration_minutes": {"value_type": "exact", "value": 130, "source_url": "https://ex.com"},
                "scored_questions": {"value_type": "exact", "value": 65, "source_url": "https://ex.com"},
                "unscored_questions": {"value_type": "unpublished", "value": None},
                "domains": [],
                "allowed_services": [],
                "retired_or_retiring_services": [],
                "out_of_scope_for_exam": [],
                "renames": [],
                "sources": [{"url": "https://ex.com", "official": True, "covers": ["price_usd"]}],
            },
        ]
    }
    (facts_dir / "two.json").write_text(json.dumps(payload), encoding="utf-8")
    return FactRegistry(load_fact_registry(str(facts_dir)))


def test_study_plan_locks_to_singular_exam_code(tmp_path):
    registry = _two_exam_registry(tmp_path)
    fm = 'slug: "azure-ai-103-developer-associate"\ntitle: "AI-103 Plan"\nexamCode: "XX-100"'
    body = "# H\n\nAn XX-100 study plan. Unlike DVA-C02, this exam is different.\n"
    exams, primary = link_exams(
        _doc("post", fm, body), "azure-ai-103-developer-associate", registry
    )
    assert [fact.code for fact in exams] == ["XX-100"]
    assert primary == {"XX-100"}


def test_unknown_exam_code_is_not_guessed(tmp_path):
    registry = _two_exam_registry(tmp_path)
    fm = 'slug: "azure-ai-103-developer-associate"\ntitle: "T"\nexamCode: "ZZ-999"'
    body = "# H\n\nThis is really an XX-100 post.\n"
    result = generate_factcheck(_doc("post", fm, body), "post", registry)
    assert result.exams == []
    assert any("not found in the fact registry" in warning for warning in result.warnings)
    assert result.new_text == _doc("post", fm, body).render()


def test_slug_role_phrase_does_not_bind_decoy(tmp_path):
    registry = _two_exam_registry(tmp_path)
    doc = _doc("post", 'slug: "azure-ai-103-developer-associate"', "# H\n\nBody.\n")
    exams, primary = link_exams(doc, "azure-ai-103-developer-associate", registry)
    assert exams == []
    assert primary == set()


def test_bound_doc_cross_reference_still_autofixes(tmp_path):
    registry = _two_exam_registry(tmp_path)
    fm = 'slug: "post"\ntitle: "T"\nexamCode: "XX-100"'
    body = "# H\n\nThe XX-100 exam costs $99.\n\nThis plan does not cover DVA-C02 at all.\n"
    result = generate_factcheck(_doc("post", fm, body), "post", registry)
    assert "$200" in result.new_text
    assert any(
        finding["category"] == "price" and finding["applied"] for finding in result.findings
    )


def test_llm_skips_unbound_exam(tmp_path, monkeypatch):
    registry = _two_exam_registry(tmp_path)
    calls = {"n": 0}

    def _fake(*args, **kwargs):
        calls["n"] += 1
        return {}

    monkeypatch.setattr("contentos.agents.factchecker.generate_fact_llm", _fake)
    doc = _doc("post", "slug: post", "# H\n\nThe XX-100 exam is hard.\n")
    generate_factcheck(doc, "post", registry, llm=object(), prompt="prompt")
    assert calls["n"] == 0
