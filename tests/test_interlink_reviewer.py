"""Deterministic reviewer tests (no LLM required)."""

from __future__ import annotations

from contentos.agents.interlink_reviewer import (
    fix_quiz_positions,
    fix_reciprocal_links,
    promote_100_to_related,
    review_mapping,
    single_related_sources,
)
from contentos.core.inventory import InventoryItem

INVENTORY = [
    InventoryItem("blog", "ready-ai-103", "Am I Ready For AI-103", "AI-103 readiness"),
    InventoryItem("blog", "top-five-ai-projects", "Top Five AI Projects For Beginners", "AI projects"),
    InventoryItem("blog", "generic-a", "Generic Article A", "Nothing specific"),
    InventoryItem("blog", "generic-b", "Generic Article B", "Nothing specific"),
]


def link(slug, position, type_="blog", text="Anchor", reason="Section"):
    return {"slug": slug, "text": text, "position": position, "type": type_, "reason": reason}


def test_reciprocal_keeps_more_relevant_direction():
    mapping = {
        "ready-ai-103": [link("top-five-ai-projects", "50%")],
        "top-five-ai-projects": [link("ready-ai-103", "50%")],
    }
    fixed, notes = fix_reciprocal_links(mapping, INVENTORY)

    assert fixed["ready-ai-103"] == []
    assert [item["slug"] for item in fixed["top-five-ai-projects"]] == ["ready-ai-103"]
    assert any("dropped ready-ai-103 -> top-five-ai-projects" in note for note in notes)


def test_reciprocal_tie_break_is_deterministic():
    mapping = {
        "generic-a": [link("generic-b", "50%")],
        "generic-b": [link("generic-a", "50%")],
    }
    fixed, _ = fix_reciprocal_links(mapping, INVENTORY)

    # Equal strength: the lexicographically smaller source slug is kept.
    assert [item["slug"] for item in fixed["generic-a"]] == ["generic-b"]
    assert fixed["generic-b"] == []


def test_reciprocal_ignores_related_links():
    mapping = {
        "generic-a": [link("generic-b", "related")],
        "generic-b": [link("generic-a", "related")],
    }
    fixed, notes = fix_reciprocal_links(mapping, INVENTORY)
    assert fixed == mapping
    assert notes == []


def test_promote_100_to_related_reorders():
    mapping = {
        "generic-a": [
            link("generic-b", "100%"),
            link("ready-ai-103", "20%"),
        ]
    }
    fixed, notes = promote_100_to_related(mapping)
    assert [item["position"] for item in fixed["generic-a"]] == ["20%", "related"]
    assert any("moved 1 link(s)" in note for note in notes)


def test_quiz_at_related_is_moved_to_free_slot():
    mapping = {"generic-a": [link("ready-ai-103", "related", type_="quiz")]}
    fixed, notes = fix_quiz_positions(mapping)
    assert fixed["generic-a"][0]["position"] == "50%"
    assert any("moved quiz" in note for note in notes)


def test_quiz_at_top_is_dropped_when_no_slot_free():
    mapping = {
        "generic-a": [
            link("ready-ai-103", "top", type_="quiz"),
            link("generic-b", "20%"),
            link("top-five-ai-projects", "50%"),
            link("generic-a", "75%"),
        ]
    }
    fixed, notes = fix_quiz_positions(mapping)
    assert all(item["type"] != "quiz" for item in fixed["generic-a"])
    assert any("dropped quiz" in note for note in notes)


def test_single_related_sources():
    mapping = {
        "generic-a": [link("generic-b", "related")],
        "generic-b": [link("generic-a", "related"), link("ready-ai-103", "related")],
    }
    assert single_related_sources(mapping) == ["generic-a"]


def test_review_mapping_applies_every_rule():
    mapping = {
        "ready-ai-103": [
            link("top-five-ai-projects", "50%"),
            link("generic-a", "100%"),
            link("ready-ai-103", "top", type_="quiz"),
        ],
        "top-five-ai-projects": [link("ready-ai-103", "50%")],
    }
    fixed, notes = review_mapping(mapping, INVENTORY)

    assert set(notes) == {"incomplete", "anchors", "reciprocal", "quiz", "promoted"}
    positions = {item["position"] for item in fixed["ready-ai-103"]}
    assert "100%" not in positions
    assert all(item["type"] != "quiz" or item["position"] in {"50%", "75%"} for item in fixed["ready-ai-103"])


def test_fix_incomplete_links_fills_and_defaults_missing_fields():
    from contentos.agents.interlink_reviewer import fix_incomplete_links

    mapping = {
        "src": [
            {"slug": "ready-ai-103"},  # missing type, text and position
            {"slug": "top-five-ai-projects", "type": "blog", "position": "50%"},  # missing text
            {"slug": "generic-a", "type": "blog", "position": "middle", "text": "ok anchor"},
        ]
    }
    fixed, notes = fix_incomplete_links(mapping, INVENTORY)
    by_slug = {item["slug"]: item for item in fixed["src"]}
    assert by_slug["ready-ai-103"]["type"] == "blog"
    assert by_slug["ready-ai-103"]["text"]
    assert by_slug["ready-ai-103"]["position"] == "related"
    assert by_slug["top-five-ai-projects"]["text"]
    assert by_slug["generic-a"]["position"] == "related"
    assert all(
        key in item for item in fixed["src"] for key in ("slug", "text", "position", "type")
    )
    assert any("filled missing type" in note for note in notes)
    assert any("filled missing anchor" in note for note in notes)


def test_fix_incomplete_links_drops_unrepairable_entries():
    from contentos.agents.interlink_reviewer import fix_incomplete_links

    mapping = {
        "src": [
            {"text": "no slug", "position": "related", "type": "blog"},
            {"slug": "unknown-slug", "position": "related"},  # no type and not in inventory
        ]
    }
    fixed, notes = fix_incomplete_links(mapping, INVENTORY)
    assert fixed["src"] == []
    assert any("missing slug" in note for note in notes)
    assert any("missing type" in note for note in notes)


def test_fix_anchor_text_capitalizes_first_letter():
    from contentos.agents.interlink_reviewer import fix_anchor_text

    mapping = {
        "src": [
            link("generic-a", "related", text="why cramming fails"),
            link("generic-b", "top", text="AI-103 study plan"),
        ]
    }
    fixed, notes = fix_anchor_text(mapping)
    texts = [item["text"] for item in fixed["src"]]
    assert texts == ["Why cramming fails", "AI-103 study plan"]
    assert any("capitalized anchor 'why cramming fails' -> 'Why cramming fails'" in note for note in notes)
    # Idempotent: a second pass changes nothing.
    again, _ = fix_anchor_text(fixed)
    assert again == fixed


def test_fix_anchor_text_drops_case_insensitive_duplicates():
    from contentos.agents.interlink_reviewer import fix_anchor_text

    mapping = {
        "src": [
            link("generic-a", "related", text="why cramming fails"),
            link("generic-b", "related", text="Why Cramming Fails"),
        ]
    }
    fixed, notes = fix_anchor_text(mapping)
    assert [item["slug"] for item in fixed["src"]] == ["generic-a"]
    assert any("duplicate anchor" in note for note in notes)
