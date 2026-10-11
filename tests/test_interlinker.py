"""Guardrail tests for the interlink agent (no LLM required)."""

from __future__ import annotations

from contentos.agents.interlinker import (
    anchor_from_title,
    finalize_links,
    sanitize_anchor,
)
from contentos.core.inventory import InventoryItem

AI103_STUDY_PLAN = "microsoft-certified-azure-ai-apps-and-agents-developer-associate-ai-103"
AI901_EXAM = "microsoft-azure-ai-fundamentals-ai-901"

INVENTORY = [
    InventoryItem("blog", "ai-103-study-guide-foundry-agents-rag", "AI-103 Study Guide 2026", "About AI-103"),
    InventoryItem("study-plan", AI103_STUDY_PLAN, "Microsoft AI-103 Study Plan | 6 Week Guide | ExamOS", "6 week AI-103 plan"),
    InventoryItem("exam", AI103_STUDY_PLAN, "Azure AI Apps and Agents Developer Associate | ExamOS", "Prepare for AI-103"),
    InventoryItem("quiz", AI103_STUDY_PLAN, "Azure AI Apps and Agents Developer Associate Practice Quiz | ExamOS", "Practice AI-103"),
    InventoryItem("exam", AI901_EXAM, "Microsoft Azure AI Fundamentals | ExamOS", "Prepare for AI-901"),
    InventoryItem("quiz", AI901_EXAM, "Microsoft Azure AI Fundamentals Practice Quiz | ExamOS", "Practice AI-901"),
    InventoryItem("roadmap", "which-certification-to-start", "Which IT Certification Should You Start With?", "Beginner guide"),
    InventoryItem("roadmap", "generative-ai-engineer", "Generative AI Engineer Roadmap 2026 | AI-103, AI-200 | ExamOS", "AI-103 path"),
]


def link(slug, type_, position, score, text="Anchor", reason="Section"):
    return {
        "slug": slug,
        "type": type_,
        "position": position,
        "score": score,
        "text": text,
        "reason": reason,
    }


def test_sanitize_anchor_strips_em_dash_and_bang():
    assert sanitize_anchor("AI-103 \u2014 Study! Guide") == "AI-103 Study Guide"


def test_anchor_from_title_is_seven_words_max():
    text = anchor_from_title("AI-103 Exam: The Eight Most Misunderstood Concepts That Trip Up Candidates")
    assert len(text.split()) <= 7


def test_below_min_score_is_dropped():
    links, warnings = finalize_links(
        [link("ai-103-study-guide-foundry-agents-rag", "blog", "20%", 20)],
        source_slug="source",
        primary_exam="AI-103",
        inventory=INVENTORY,
    )
    assert all(
        not (
            item["slug"] == "ai-103-study-guide-foundry-agents-rag"
            and item["position"] in {"20%", "50%", "75%"}
        )
        for item in links
    )
    assert any("below 40" in warning for warning in warnings)


def test_self_link_is_dropped():
    links, warnings = finalize_links(
        [link("which-certification-to-start", "roadmap", "related", 60)],
        source_slug="which-certification-to-start",
        primary_exam=None,
        inventory=INVENTORY,
    )
    assert all(item["slug"] != "which-certification-to-start" for item in links)
    assert any("self-link" in warning for warning in warnings)


def test_unknown_or_type_mismatch_is_dropped():
    links, warnings = finalize_links(
        [link("ai-103-study-guide-foundry-agents-rag", "exam", "50%", 90)],
        source_slug="source",
        primary_exam="AI-103",
        inventory=INVENTORY,
    )
    assert all(
        not (item["slug"] == "ai-103-study-guide-foundry-agents-rag" and item["type"] == "exam")
        for item in links
    )
    assert any("unknown inventory item" in warning for warning in warnings)


def test_top_cannot_be_exam_and_falls_back():
    links, warnings = finalize_links(
        [
            link(AI103_STUDY_PLAN, "exam", "top", 100),
            link("ai-103-study-guide-foundry-agents-rag", "blog", "50%", 60),
        ],
        source_slug="source",
        primary_exam="AI-103",
        inventory=INVENTORY,
    )
    top = next(item for item in links if item["position"] == "top")
    assert top["type"] == "study-plan"
    assert any("top must not be an exam" in warning for warning in warnings)


def test_quiz_only_for_primary_exam_and_correct_position():
    links, warnings = finalize_links(
        [
            link(AI103_STUDY_PLAN, "quiz", "20%", 100, text="AI-103 Quiz"),
            link(AI901_EXAM, "quiz", "50%", 100, text="AI-901 Quiz"),
        ],
        source_slug="source",
        primary_exam="AI-103",
        inventory=INVENTORY,
    )
    # Quizzes may sit at 20/50/75 now; a non-primary exam's quiz is still dropped.
    assert any(item["type"] == "quiz" and item["slug"] == AI103_STUDY_PLAN for item in links)
    assert all(not (item["type"] == "quiz" and item["slug"] == AI901_EXAM) for item in links)
    assert any("not the primary exam" in warning for warning in warnings)


def test_inline_conflict_keeps_higher_score():
    links, warnings = finalize_links(
        [
            link("ai-103-study-guide-foundry-agents-rag", "blog", "50%", 60, text="Low"),
            link("generative-ai-engineer", "roadmap", "50%", 80, text="High"),
        ],
        source_slug="source",
        primary_exam="AI-103",
        inventory=INVENTORY,
    )
    inline = [item for item in links if item["position"] == "50%"]
    assert len(inline) == 1
    assert inline[0]["text"] == "High"
    assert any("taken by" in warning for warning in warnings)


def test_duplicate_anchor_text_is_dropped():
    links, warnings = finalize_links(
        [
            link("ai-103-study-guide-foundry-agents-rag", "blog", "20%", 60, text="Same Text"),
            link("generative-ai-engineer", "roadmap", "75%", 60, text="Same Text"),
        ],
        source_slug="source",
        primary_exam="AI-103",
        inventory=INVENTORY,
    )
    texts = [item["text"] for item in links]
    assert len(texts) == len(set(texts))
    assert any("duplicate anchor text" in warning for warning in warnings)


def test_related_drops_quiz_when_exam_present():
    links, _ = finalize_links(
        [
            link(AI103_STUDY_PLAN, "exam", "related", 100, text="AI-103 Exam"),
            link(AI103_STUDY_PLAN, "quiz", "related", 100, text="AI-103 Quiz"),
        ],
        source_slug="source",
        primary_exam="AI-103",
        inventory=INVENTORY,
    )
    related = [item for item in links if item["position"] == "related"]
    assert any(item["type"] == "exam" for item in related)
    assert all(item["type"] != "quiz" for item in related)


def test_output_links_only_contain_public_fields():
    links, _ = finalize_links(
        [link("ai-103-study-guide-foundry-agents-rag", "blog", "related", 60)],
        source_slug="source",
        primary_exam="AI-103",
        inventory=INVENTORY,
    )
    for item in links:
        assert set(item) == {"slug", "text", "position", "type", "reason"}


def test_banned_anchor_phrase_is_dropped():
    links, warnings = finalize_links(
        [link("ai-103-study-guide-foundry-agents-rag", "blog", "20%", 60, text="Read more")],
        source_slug="source",
        primary_exam="AI-103",
        inventory=INVENTORY,
    )
    assert all(item["text"].lower() != "read more" for item in links)
    assert any("banned anchor" in warning for warning in warnings)


def test_100_percent_is_promoted_to_related():
    links, warnings = finalize_links(
        [link("ai-103-study-guide-foundry-agents-rag", "blog", "100%", 60)],
        source_slug="source",
        primary_exam="AI-103",
        inventory=INVENTORY,
    )
    promoted = [item for item in links if item["slug"] == "ai-103-study-guide-foundry-agents-rag"]
    assert len(promoted) == 1
    assert promoted[0]["position"] == "related"
    assert any("100% to related" in warning for warning in warnings)


def test_sanitize_anchor_capitalizes_the_first_letter():
    assert sanitize_anchor("why cramming fails") == "Why cramming fails"
    assert sanitize_anchor("daily practice versus cramming") == "Daily practice versus cramming"
    # Idempotent, and a leading exam code or number is left alone.
    assert sanitize_anchor("Why cramming fails") == "Why cramming fails"
    assert sanitize_anchor("AI-103 study plan") == "AI-103 study plan"
    assert sanitize_anchor("3 ways to study") == "3 ways to study"
    assert sanitize_anchor('"why cramming fails"') == '"Why cramming fails"'


def test_examcodes_force_the_first_quiz_at_an_inline_slot():
    links, _ = finalize_links(
        [],
        source_slug="source",
        primary_exam="AI-103",
        inventory=INVENTORY,
        exam_codes=["AI-103"],
    )
    quizzes = [item for item in links if item["type"] == "quiz"]
    assert len(quizzes) == 1
    assert quizzes[0]["slug"] == AI103_STUDY_PLAN
    assert quizzes[0]["position"] in {"20%", "50%", "75%"}
    # the exam page is lower priority and lands in related
    exams = [item for item in links if item["type"] == "exam"]
    assert exams and exams[0]["position"] == "related"


def test_two_examcodes_force_two_quizzes():
    links, _ = finalize_links(
        [],
        source_slug="source",
        primary_exam="AI-103",
        inventory=INVENTORY,
        exam_codes=["AI-103", "AI-901"],
    )
    quizzes = {item["slug"] for item in links if item["type"] == "quiz"}
    assert quizzes == {AI103_STUDY_PLAN, AI901_EXAM}
    positions = [item["position"] for item in links if item["type"] == "quiz"]
    assert len(positions) == len(set(positions))


def test_top_prefers_study_plan_for_the_primary_exam():
    links, _ = finalize_links(
        [link("ai-103-study-guide-foundry-agents-rag", "blog", "top", 60)],
        source_slug="source",
        primary_exam="AI-103",
        inventory=INVENTORY,
        exam_codes=["AI-103"],
    )
    top = next(item for item in links if item["position"] == "top")
    assert top["type"] in {"study-plan", "roadmap"}


def test_related_is_capped_at_nine():
    inventory = [
        InventoryItem("blog", f"blog-{i}", f"Generic Blog {i}", f"AI-103 topic {i}")
        for i in range(12)
    ]
    raw = [link(f"blog-{i}", "blog", "related", 60, text=f"Item {i}") for i in range(12)]
    links, _ = finalize_links(
        raw, source_slug="source", primary_exam=None, inventory=inventory
    )
    related = [item for item in links if item["position"] == "related"]
    assert len(related) == 9
