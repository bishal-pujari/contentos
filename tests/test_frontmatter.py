"""Frontmatter validator/corrector tests (no network)."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from contentos.agents.frontmatter import (
    ExamEntry,
    correct_frontmatter,
    detect_exam_codes,
    load_frontmatter_schema,
    load_seo_spec,
    parse_frontmatter,
)
from contentos.core import pipeline
from contentos.core.loaders import parse_document

PROMPT = Path(__file__).resolve().parents[1] / "prompts" / "examos-blog-frontmatter.md"
STUDY_PLAN_PROMPT = (
    Path(__file__).resolve().parents[1] / "prompts" / "examos-study-plan-frontmatter.md"
)
ROADMAP_PROMPT = Path(__file__).resolve().parents[1] / "prompts" / "examos-roadmap-frontmatter.md"
SCHEMA = load_frontmatter_schema(str(PROMPT))
REGISTRY = [
    ExamEntry("AI-103", "Azure AI Apps and Agents Developer Associate"),
    ExamEntry("SA_6", "SAFe Agilist 6.0"),
    ExamEntry("SY0-701", "CompTIA Security+"),
    ExamEntry("AZ-104", "Azure Administrator Associate"),
]


def _doc(slug, frontmatter, body="# H1\n\nBody paragraph.\n"):
    text = f"---\n{frontmatter}\n---\n{body}"
    return parse_document(text, f"{slug}.mdx")


def _fm(result):
    return result.new_text.split("---")[1].strip("\n")


def test_schema_loads_from_markdown():
    keys = [spec.key for spec in SCHEMA.fields]
    assert keys[:5] == ["slug", "title", "seoTitle", "seoDescription", "type"]
    assert keys[-1] == "targetAudience"
    field_map = SCHEMA.field_map
    assert field_map["type"].const == "post"
    assert field_map["imagePosition"].const == "top"
    assert field_map["title"].minimum == 50 and field_map["title"].maximum == 65
    assert field_map["order"].default == 900
    assert field_map["provider"].only == "study_plan"
    assert "Sarah Chen" in SCHEMA.author_names
    assert "project-management" in SCHEMA.categories
    assert SCHEMA.aliases["examCode"] == "examCodes"
    assert SCHEMA.aliases["seo_title"] == "seoTitle"


def test_reorders_fields_and_quotes_values():
    doc = _doc(
        "post",
        'title: "A Title"\nslug: post\norder: 12\ntype: post\n'
        'imagePosition: "top"\nauthor: "Sarah Chen"\ncategories: ["cloud"]\n'
        'date: "2026-01-01"\nseoDescription: "Desc"\nseoTitle: "SEO Title"',
    )
    result = correct_frontmatter(doc, SCHEMA)
    lines = [line.split(":")[0] for line in _fm(result).splitlines()]
    expected = [spec.key for spec in SCHEMA.fields if spec.required and spec.only != "study_plan"]
    assert lines == expected
    assert 'type: "post"' in result.new_text
    assert 'slug: "post"' in result.new_text
    assert any(entry["field"] == "(order)" for entry in result.changes)


def test_renames_aliases_and_drops_duplicates():
    doc = _doc(
        "post",
        'slug: post\ntitle: "T"\nseo_title: "ST"\nseoDescription: "D"\ntype: post\n'
        'imagePosition: top\nauthor: "Sarah Chen"\nauthor: "Alex Morgan"\n'
        'categories: ["ai"]\ndate: "2026-01-01"\nexamCode: ["AI-103"]',
    )
    result = correct_frontmatter(doc, SCHEMA)
    fm = _fm(result)
    assert "seo_title" not in fm and 'seoTitle: "ST"' in fm
    assert fm.count("author:") == 1
    assert "examCodes: [\"AI-103\"]" in fm
    assert any("duplicate" in note for note in result.notes)


def test_invalid_author_replaced_with_closest():
    doc = _doc(
        "post",
        'slug: post\ntitle: "T"\nseoTitle: "ST"\nseoDescription: "D"\ntype: post\n'
        'imagePosition: top\nauthor: "Praveen Sharma"\ncategories: ["project-management"]\n'
        'date: "2026-01-01"',
    )
    result = correct_frontmatter(doc, SCHEMA)
    assert 'author: "Pradeep Sharma"' in result.new_text
    assert any("not approved" in item for item in result.review_items)


def test_categories_mapped_sorted_and_dropped():
    doc = _doc(
        "post",
        'slug: post\ntitle: "T"\nseoTitle: "ST"\nseoDescription: "D"\ntype: post\n'
        'imagePosition: top\nauthor: "Sarah Chen"\ndate: "2026-01-01"\n'
        'categories: ["pmp", "administration", "security"]',
    )
    result = correct_frontmatter(doc, SCHEMA)
    assert 'categories: ["project-management", "security"]' in result.new_text
    assert any("administration" in item for item in result.review_items)
    assert any("pmp" in item and "project-management" in item for item in result.review_items)


def test_const_fields_forced():
    doc = _doc(
        "post",
        'slug: post\ntitle: "T"\nseoTitle: "ST"\nseoDescription: "D"\ntype: article\n'
        'imagePosition: bottom\nauthor: "Sarah Chen"\ncategories: ["cloud"]\ndate: "2026-01-01"',
    )
    result = correct_frontmatter(doc, SCHEMA)
    assert 'type: "post"' in result.new_text
    assert 'imagePosition: "top"' in result.new_text
    assert "article" not in result.new_text


def test_missing_author_inferred_and_missing_date_defaulted():
    doc = _doc(
        "post",
        'slug: post\ntitle: "T"\nseoTitle: "ST"\nseoDescription: "D"\ntype: post\n'
        'imagePosition: top\ncategories: ["ai"]',
    )
    result = correct_frontmatter(doc, SCHEMA, today="2026-05-05")
    assert 'author: "Alex Morgan"' in result.new_text
    assert 'date: "2026-05-05"' in result.new_text
    assert any("author" in note for note in result.notes)


def test_slug_corrected_to_filename():
    doc = _doc(
        "correct-name",
        'slug: wrong-name\ntitle: "T"\nseoTitle: "ST"\nseoDescription: "D"\ntype: post\n'
        'imagePosition: top\nauthor: "Sarah Chen"\ncategories: ["cloud"]\ndate: "2026-01-01"',
    )
    result = correct_frontmatter(doc, SCHEMA)
    assert 'slug: "correct-name"' in result.new_text


def test_study_plan_fields_removed_from_blog():
    doc = _doc(
        "post",
        'slug: post\ntitle: "T"\nseoTitle: "ST"\nseoDescription: "D"\ntype: post\n'
        'imagePosition: top\nauthor: "Sarah Chen"\ncategories: ["cloud"]\ndate: "2026-01-01"\n'
        'provider: "Microsoft"\nestimatedHours: 40',
    )
    result = correct_frontmatter(doc, SCHEMA)
    assert "provider" not in result.new_text
    assert "estimatedHours" not in result.new_text
    assert any("study-plan field" in note for note in result.notes)


def test_only_order_changes_nothing_else():
    doc = _doc(
        "post",
        'slug: post\ntitle: "T"\norder: 5\ntype: post\ncategories: ["cloud"]',
    )
    result = correct_frontmatter(doc, SCHEMA, order_map={"post": 99}, only_order=True)
    assert "order: 99" in result.new_text
    assert result.new_text.split("---", 2)[2] == doc.render().split("---", 2)[2]
    # Everything else keeps its original (unquoted) formatting.
    assert 'slug: post' in result.new_text
    assert 'slug: "post"' not in result.new_text


def test_order_file_applied_in_full_pass():
    doc = _doc(
        "post",
        'slug: post\ntitle: "T"\nseoTitle: "ST"\nseoDescription: "D"\ntype: post\n'
        'imagePosition: top\nauthor: "Sarah Chen"\ncategories: ["cloud"]\ndate: "2026-01-01"\norder: 5',
    )
    result = correct_frontmatter(doc, SCHEMA, order_map={"post": 42})
    assert "order: 42" in result.new_text
    assert any(
        entry["field"] == "order" and entry["reason"] == "order file" for entry in result.changes
    )


def test_unknown_fields_preserved_and_flagged():
    doc = _doc(
        "post",
        'slug: post\ntitle: "T"\nseoTitle: "ST"\nseoDescription: "D"\ntype: post\n'
        'imagePosition: top\nauthor: "Sarah Chen"\ncategories: ["cloud"]\ndate: "2026-01-01"\n'
        'customField: "keep me"',
    )
    result = correct_frontmatter(doc, SCHEMA)
    assert 'customField: "keep me"' in result.new_text
    assert any("customField" in item for item in result.review_items)


def test_parse_frontmatter_keeps_first_duplicate():
    values, order, duplicates = parse_frontmatter('author: "A"\nauthor: "B"\nslug: x')
    assert values["author"] == "A"
    assert order == ["author", "slug"]
    assert duplicates == ["author"]


def test_yaml_frontmatter_schema_override(tmp_path):
    prompt = tmp_path / "custom.md"
    prompt.write_text(
        "---\n"
        "name: Custom\n"
        "fields:\n"
        "  - {key: slug, required: true, type: text}\n"
        "  - {key: title, required: true, type: text, min: 10, max: 20}\n"
        "  - {key: type, required: true, type: text, const: article}\n"
        "  - {key: order, required: true, type: int, default: 5}\n"
        "authors:\n"
        "  Jane Doe: {specialty: General, categories: [general]}\n"
        "categories: [general]\n"
        "exam_codes: [XX-100]\n"
        "aliases:\n"
        "  seo_title: title\n"
        "---\n"
        "System prompt body.\n",
        encoding="utf-8",
    )
    schema = load_frontmatter_schema(str(prompt))
    assert schema.name == "Custom"
    assert schema.field_map["type"].const == "article"
    assert schema.field_map["order"].default == 5
    assert schema.author_names == ["Jane Doe"]

    doc = _doc("post", "slug: post\ntitle: A Long Enough Title\nseo_title: ignored")
    result = correct_frontmatter(doc, schema)
    assert 'type: "article"' in result.new_text
    assert 'order: 5' in result.new_text
    assert "seo_title" not in result.new_text


def test_run_fix_frontmatter_dry_run_and_apply(tmp_path):
    blog = tmp_path / "blog"
    blog.mkdir()
    (blog / "post.mdx").write_text(
        '---\nslug: post\ntitle: "T"\nseoTitle: "ST"\nseoDescription: "D"\n'
        'type: post\ncategories: ["cloud"]\nauthor: "Sarah Chen"\n'
        'order: 5\nimagePosition: top\ndate: "2026-01-01"\n---\n# Post\n\nBody.\n',
        encoding="utf-8",
    )
    dry = pipeline.run_fix_frontmatter(
        input_dir=str(blog),
        output_dir=str(tmp_path / "out"),
        reports_dir=str(tmp_path / "reports"),
        prompt_path=str(PROMPT),
        seo=False,
        workers=1,
    )
    assert dry.files_scanned == 1
    assert dry.files_changed == 1
    assert not (tmp_path / "out").exists()
    payload = json.loads(Path(dry.extra["json_report"]).read_text(encoding="utf-8"))
    assert payload["agent"] == "fix_frontmatter"
    assert Path(dry.extra["markdown_report"]).exists()

    applied = pipeline.run_fix_frontmatter(
        input_dir=str(blog),
        output_dir=str(tmp_path / "out"),
        reports_dir=str(tmp_path / "reports"),
        prompt_path=str(PROMPT),
        seo=False,
        apply=True,
        workers=1,
    )
    assert applied.files_changed == 1
    corrected = (tmp_path / "out" / "post.mdx").read_text(encoding="utf-8")
    assert '# Post\n\nBody.\n' in corrected
    lines = [line.split(":")[0] for line in corrected.split("---")[1].strip().splitlines()]
    expected = [spec.key for spec in SCHEMA.fields if spec.required and spec.only != "study_plan"]
    assert lines == expected


SEO_PROMPT = Path(__file__).resolve().parents[1] / "prompts" / "examos_blog_seo_rewritre.md"
SEO_SPEC = load_seo_spec(str(SEO_PROMPT))


class _Response:
    def __init__(self, content):
        self.content = content


class _SeoLlm:
    def __init__(self, payload):
        self.payload = payload

    def invoke(self, messages):
        return _Response(json.dumps(self.payload))


def _long_seo_doc():
    return _doc(
        "post",
        'slug: post\ntitle: "' + ("word " * 20).strip() + '"\n'
        'seoTitle: "' + ("title " * 15).strip() + '"\n'
        'seoDescription: "' + ("detail " * 30).strip() + '"\n'
        'type: post\nimagePosition: top\nauthor: "Sarah Chen"\ncategories: ["cloud"]\ndate: "2026-01-01"',
    )


def test_out_of_range_seo_fields_are_flagged_without_llm():
    result = correct_frontmatter(_long_seo_doc(), SCHEMA, detect_exams=False)
    assert any("needs shortening" in item for item in result.review_items)
    assert "trimmed" not in " ".join(result.notes)


def test_seo_rewrite_applies_valid_candidates():
    proposal = {
        "title": "AI-103 Study Guide: The Concepts That Trip Candidates",
        "seoTitle": "AI-103 Exam Guide 2026: What Every Candidate Should Know",
        "seoDescription": (
            "AI-103 covers agents, RAG, evaluation, and deployment. This guide explains "
            "what to study first and how to prepare efficiently for the exam today."
        ),
    }
    result = correct_frontmatter(
        _long_seo_doc(),
        SCHEMA,
        detect_exams=False,
        seo_llm=_SeoLlm(proposal),
        seo_spec=SEO_SPEC,
    )
    values, _, _ = parse_frontmatter(_fm(result))
    assert values["title"] == proposal["title"]
    assert values["seoTitle"] == proposal["seoTitle"]
    assert values["seoDescription"] == proposal["seoDescription"]
    assert not any("needs shortening" in item for item in result.review_items)
    assert any("rewrote title" in note for note in result.notes)


def test_seo_rewrite_rejects_out_of_range_candidate():
    proposal = {
        "title": "Too Short",
        "seoTitle": "Okay Title For The Exam Page 2026",
        "seoDescription": "AI-103 short description.",
    }
    result = correct_frontmatter(
        _long_seo_doc(),
        SCHEMA,
        detect_exams=False,
        seo_llm=_SeoLlm(proposal),
        seo_spec=SEO_SPEC,
    )
    values, _, _ = parse_frontmatter(_fm(result))
    assert values["title"] != "Too Short"  # rejected, original kept
    assert any("rejected" in warning for warning in result.warnings)


def test_under_length_seo_field_is_flagged():
    doc = _doc(
        "post",
        'slug: post\ntitle: "Short"\nseoTitle: "Short"\nseoDescription: "Short"\n'
        'type: post\nimagePosition: top\nauthor: "Sarah Chen"\ncategories: ["cloud"]\ndate: "2026-01-01"',
    )
    result = correct_frontmatter(doc, SCHEMA, detect_exams=False)
    assert any("needs expansion" in item for item in result.review_items)


def _detect(slug, frontmatter, body):
    doc = _doc(slug, frontmatter, body)
    values, _, _ = parse_frontmatter(frontmatter)
    return detect_exam_codes(SCHEMA, doc, slug, values, REGISTRY)


BASE_FM = 'slug: post\ntitle: "T"\nseoTitle: "ST"\nseoDescription: "D"\ntype: post\nimagePosition: top'


def test_detect_exam_code_from_body():
    assert _detect("post", BASE_FM, "# H\n\nThis guide covers AI-103 today.\n") == ["AI-103"]


def test_detect_exam_from_common_cert_name_in_body():
    registry = [
        ExamEntry("CLF-C02", "AWS Certified Cloud Practitioner"),
        ExamEntry("SAP-C02", "AWS Certified Solutions Architect - Professional"),
        ExamEntry("SAA-C03", "AWS Certified Solutions Architect - Associate"),
    ]
    body = (
        "# H\n\nAWS certifications, from Cloud Practitioner to Solutions Architect "
        "Professional, share a common pattern.\n"
    )
    doc = _doc("post", BASE_FM, body)
    values, _, _ = parse_frontmatter(BASE_FM)
    codes = detect_exam_codes(SCHEMA, doc, "post", values, registry)
    assert "CLF-C02" in codes
    assert "SAP-C02" in codes


def test_detect_exam_from_name_alias_in_body():
    result = _detect("post", BASE_FM, "# H\n\nStudy for SAFe Agilist this year.\n")
    assert result == ["SA_6"]


def test_detect_exam_from_slug_metadata():
    assert _detect("how-hard-is-security-plus", BASE_FM, "# H\n\nNo cert code here.\n") == ["SY0-701"]


def test_curated_alias_detects_gcp_pca():
    result = _detect(
        "pca-exam-case-study-guide",
        BASE_FM,
        "# H\n\nThe Google Professional Cloud Architect exam uses case studies.\n",
    )
    assert result == ["GCP-PCA"]


def test_body_conjunction_is_not_an_exam_name():
    # "Security + Data" must not be mistaken for CompTIA Security+.
    assert _detect("post", BASE_FM, "# H\n\nManaged Identity covers Security + Data + Monitoring.\n") == []


def test_detected_exam_codes_merge_with_existing_and_drop_invalid():
    fm = BASE_FM + '\nexamCodes: ["AI-103", "ZZ-999"]'
    doc = _doc("post", fm, "# H\n\nStudy SAFe Agilist.\n")
    result = correct_frontmatter(doc, SCHEMA, exam_registry=REGISTRY)
    assert 'examCodes: ["AI-103", "SA_6"]' in result.new_text
    assert any("ZZ-999" in item for item in result.review_items)


def test_run_fix_frontmatter_fills_exam_codes(tmp_path):
    blog = tmp_path / "blog"
    blog.mkdir()
    (blog / "post.mdx").write_text(
        '---\nslug: post\ntitle: "AI-103" \nseoTitle: "AI-103 Exam Guide"\n'
        'seoDescription: "AI-103 exam guide"\ntype: post\nimagePosition: top\n'
        'author: "Sarah Chen"\ncategories: ["ai"]\ndate: "2026-01-01"\n---\n'
        "# AI-103\n\nThis post covers AI-103.\n",
        encoding="utf-8",
    )
    db = tmp_path / "exam_db.json"
    db.write_text(json.dumps([{"code": "AI-103", "name": "Azure AI Apps and Agents Developer Associate"}]), encoding="utf-8")
    report = pipeline.run_fix_frontmatter(
        input_dir=str(blog),
        output_dir=str(tmp_path / "out"),
        reports_dir=str(tmp_path / "reports"),
        prompt_path=str(PROMPT),
        exam_db=str(db),
        seo=False,
        apply=True,
        workers=1,
    )
    assert report.files_scanned == 1
    corrected = (tmp_path / "out" / "post.mdx").read_text(encoding="utf-8")
    assert 'examCodes: ["AI-103"]' in corrected


def test_run_only_order_reports_missing_order_slugs(tmp_path):
    blog = tmp_path / "blog"
    blog.mkdir()
    (blog / "post.mdx").write_text(
        '---\nslug: post\ntitle: "T"\norder: 5\n---\n# Post\n\nBody.\n', encoding="utf-8"
    )
    order = tmp_path / "order.json"
    order.write_text(json.dumps({"post": 10, "missing-post": 20}), encoding="utf-8")

    report = pipeline.run_fix_frontmatter(
        input_dir=str(blog),
        reports_dir=str(tmp_path / "reports"),
        prompt_path=str(PROMPT),
        order_file=str(order),
        only_order=True,
        workers=1,
    )
    assert report.extra["missing_order_slugs"] == ["missing-post"]
    assert "order: 10" in report.changes[0].diff


def _detect_with(slug, frontmatter, body, registry):
    doc = _doc(slug, frontmatter, body)
    values, _, _ = parse_frontmatter(frontmatter)
    return detect_exam_codes(SCHEMA, doc, slug, values, registry)


VENDOR_REGISTRY = [
    ExamEntry("DVA-C02", "AWS Certified Developer - Associate"),
    ExamEntry("AZ-204", "Azure Developer Associate"),
    ExamEntry("SY0-701", "CompTIA Security+"),
]


def test_vendor_stripped_alias_requires_its_vendor():
    # "Azure Developer Associate" must not tag the AWS DVA-C02 exam.
    codes = _detect_with(
        "post",
        BASE_FM,
        "# H\n\nThe Azure Developer Associate role builds on Azure SDK skills.\n",
        VENDOR_REGISTRY,
    )
    assert "AZ-204" in codes
    assert "DVA-C02" not in codes


def test_vendor_stripped_alias_matches_when_vendor_is_present():
    codes = _detect_with(
        "post",
        BASE_FM,
        "# H\n\nAWS Developer Associate is the entry AWS developer cert.\n",
        VENDOR_REGISTRY,
    )
    assert "DVA-C02" in codes


def test_lone_cross_vendor_code_mention_is_not_tagged():
    fm = 'slug: az-204-replacement\ntitle: "AZ-204 Retired"\nseoTitle: "ST"\nseoDescription: "D"\ntype: post\nimagePosition: top'
    body = (
        "# H\n\nMost retirements are version bumps. "
        "Security+ SY0-601 became SY0-701 without changing discipline.\n"
    )
    codes = _detect_with("az-204-replacement", fm, body, VENDOR_REGISTRY)
    assert "AZ-204" in codes
    assert "SY0-701" not in codes


def test_cross_vendor_code_in_a_heading_is_kept():
    fm = 'slug: az-204-replacement\ntitle: "AZ-204 Retired"\nseoTitle: "ST"\nseoDescription: "D"\ntype: post\nimagePosition: top'
    body = "# H\n\n## CompTIA Security+ (SY0-701)\n\nA short comparison section.\n"
    codes = _detect_with("az-204-replacement", fm, body, VENDOR_REGISTRY)
    assert "SY0-701" in codes


STUDY_PLAN_SCHEMA = load_frontmatter_schema(str(STUDY_PLAN_PROMPT))
ROADMAP_SCHEMA = load_frontmatter_schema(str(ROADMAP_PROMPT))


def test_study_plan_schema_uses_examcode_singular():
    assert STUDY_PLAN_SCHEMA.document_type == "study_plan"
    keys = [spec.key for spec in STUDY_PLAN_SCHEMA.fields]
    assert keys[:3] == ["slug", "provider", "title"]
    assert "examCode" in keys
    assert "examCodes" not in keys
    assert STUDY_PLAN_SCHEMA.field_map["passingScore"].type == "text"
    assert STUDY_PLAN_SCHEMA.field_map["estimatedHours"].type == "int"


def test_study_plan_correct_keeps_examcode_and_passing_score():
    fm = (
        "slug: plan\n"
        "provider: Microsoft\n"
        'title: "Azure Administrator Associate (AZ-104) Study Plan"\n'
        'description: "An eight week plan covering identities, storage, compute, networking and monitoring."\n'
        'seoTitle: "AZ-104 Study Plan for 2026"\n'
        'seoDescription: "An eight week AZ-104 plan covering Entra ID, storage, compute, networking and monitoring."\n'
        'author: "Priya Krishnamurthy"\n'
        'date: "2026-07-18"\n'
        'examCode: "AZ-104"\n'
        "estimatedHours: 50\n"
        'passingScore: "700 / 1000"\n'
        'targetAudience: "Azure administrators with hands-on experience"\n'
        'categories: ["azure", "cloud"]\n'
        "order: 10\n"
        'imagePosition: "top"'
    )
    doc = _doc("plan", fm, "# Plan\n\nBody.\n")
    result = correct_frontmatter(doc, STUDY_PLAN_SCHEMA)
    parsed = yaml.safe_load(result.new_text.split("---")[1])
    assert parsed["examCode"] == "AZ-104"
    assert parsed["passingScore"] == "700 / 1000"
    assert parse_document(result.new_text).body == doc.body


def test_roadmap_schema_preserves_nested_fields_and_keeps_provider():
    assert ROADMAP_SCHEMA.document_type == "roadmap"
    fm = (
        "slug: rm\n"
        'date: "2026-01-01"\n'
        "provider: AWS\n"
        'title: "AWS Solutions Architect Roadmap"\n'
        'seoTitle: "AWS Solutions Architect Roadmap 2026"\n'
        'seoDescription: "A career roadmap from AWS fundamentals to professional solutions architecture."\n'
        'cardSummary: "Earn the AWS Solutions Architect certifications."\n'
        'description: "A guided path through SAA-C03 and SAP-C02 for aspiring architects."\n'
        'audience: "Cloud engineers moving into architecture."\n'
        'totalTime: "~8-14 months"\n'
        "order: 80\n"
        'categories: ["architecture", "aws"]\n'
        "skills:\n  - name: AWS Infrastructure\n    level: 4\n"
        "steps:\n  - title: Step 0\n    topics:\n      - networking\n"
    )
    doc = _doc("rm", fm, "# Roadmap\n\nBody.\n")
    result = correct_frontmatter(doc, ROADMAP_SCHEMA, nested_preserve=ROADMAP_SCHEMA_NESTED)
    assert "skills:\n  - name: AWS Infrastructure\n    level: 4" in result.new_text
    parsed = yaml.safe_load(result.new_text.split("---")[1])
    assert parsed["skills"] == [{"name": "AWS Infrastructure", "level": 4}]
    assert parsed["steps"] == [{"title": "Step 0", "topics": ["networking"]}]
    assert parsed["provider"] == "AWS"
    assert parse_document(result.new_text).body == doc.body


ROADMAP_SCHEMA_NESTED = (
    "skills",
    "certificationPath",
    "milestones",
    "steps",
    "finalCerts",
    "timeline",
    "honestTimeline",
    "finalSectionTitle",
    "finalSectionBody",
)
