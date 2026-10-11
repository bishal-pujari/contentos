"""Interlink reviewer: deterministic post-processing of an existing mapping.

The interlink agent produces ``interlinks.json``; this module repairs the three
classes of problems that survive a run, without asking the LLM to write anything:

1. **Reciprocal inline links** — when source A links to source B inline and B
   links back to A inline, the weaker direction is dropped. Because the score
   from the original run is not persisted, a deterministic proxy ranks each
   direction by exam relevance, position prominence and target type.
2. **100% links** — an end-of-article link behaves like a Related Reading item,
   so every ``100%`` link is promoted to ``related``.
3. **Misplaced quizzes** — a quiz may only sit at ``50%`` or ``75%``; one found
   at ``top`` or ``related`` is moved to a free inline slot or dropped.

Re-running the LLM for sources that ended up with a single Related item is a
pipeline concern (``run_review_interlinks``), not part of this module.
"""

from __future__ import annotations

from contentos.agents.interlinker import (
    INLINE_POSITIONS,
    POSITION_ORDER,
    POSITIONS,
    QUIZ_POSITIONS,
    QUIZ_SLOT_ORDER,
    anchor_from_title,
    capitalize_anchor,
)
from contentos.core.inventory import InventoryItem, exam_codes, index_by_slug

# Higher is more prominent / more likely to be kept when breaking a tie.
POSITION_PROMINENCE = {"top": 6, "20%": 5, "50%": 4, "75%": 3, "100%": 2, "related": 1}
TYPE_RANK = {"study-plan": 5, "roadmap": 5, "exam": 4, "quiz": 4, "blog": 3}


def _slug_codes(slug: str, index: dict[str, list[InventoryItem]]) -> set[str]:
    codes: set[str] = set()
    for item in index.get(slug, ()):  # type: ignore[arg-type]
        codes |= exam_codes(f"{item.slug} {item.seo_title} {item.seo_description}")
    return codes


def _relevance(
    source_slug: str, target_slug: str, index: dict[str, list[InventoryItem]]
) -> int:
    """Deterministic proxy for the LLM's 0-100 relevance score (higher wins).

    Mirrors the rubric's big steps: an exact exam match is strongest, a generic
    article linking to an exam-specific one is still useful, and a specific
    article pointing at an unrelated generic one is weakest.
    """
    source_codes = _slug_codes(source_slug, index)
    target_codes = _slug_codes(target_slug, index)
    if source_codes and source_codes & target_codes:
        return 100
    if source_codes and target_codes:
        return 40
    if source_codes and not target_codes:
        return 30
    if target_codes and not source_codes:
        return 40
    return 35


def _strength(
    source_slug: str,
    target_slug: str,
    position: str,
    type_: str,
    index: dict[str, list[InventoryItem]],
) -> tuple[int, int, int]:
    return (
        _relevance(source_slug, target_slug, index),
        POSITION_PROMINENCE.get(position, 0),
        TYPE_RANK.get(type_, 0),
    )


def fix_reciprocal_links(
    mapping: dict[str, list[dict]],
    inventory: list[InventoryItem],
) -> tuple[dict[str, list[dict]], list[str]]:
    """Drop the weaker direction of every reciprocal inline pair.

    Only inline positions participate; a Related Reading entry pointing back at a
    source is normal and is left alone.
    """
    index = index_by_slug(inventory)
    inline: dict[tuple[str, str], dict] = {}
    for source, links in mapping.items():
        for link in links:
            if link.get("position") in INLINE_POSITIONS:
                inline[(source, link["slug"])] = link

    dropped: set[tuple[str, str]] = set()
    notes: list[str] = []
    for (source, target), link in inline.items():
        if source >= target or (target, source) not in inline:
            continue
        back = inline[(target, source)]
        forward = _strength(source, target, link["position"], link["type"], index)
        backward = _strength(target, source, back["position"], back["type"], index)
        if forward > backward or (forward == backward and source < target):
            dropped.add((target, source))
            kept, lost = (source, target), (target, source)
        else:
            dropped.add((source, target))
            kept, lost = (target, source), (source, target)
        notes.append(
            f"reciprocal inline: kept {kept[0]} -> {kept[1]}, dropped {lost[0]} -> {lost[1]}"
        )

    if not dropped:
        return mapping, notes

    cleaned: dict[str, list[dict]] = {}
    for source, links in mapping.items():
        cleaned[source] = [
            link
            for link in links
            if not (
                (source, link["slug"]) in dropped
                and link.get("position") in INLINE_POSITIONS
            )
        ]
    return cleaned, notes


def fix_quiz_positions(
    mapping: dict[str, list[dict]],
) -> tuple[dict[str, list[dict]], list[str]]:
    """Move a misplaced quiz to a free 50%/75% slot, or drop it if none is free."""
    cleaned: dict[str, list[dict]] = {}
    notes: list[str] = []
    for source, links in mapping.items():
        occupied = {link["position"] for link in links if link["position"] in QUIZ_POSITIONS}
        kept: list[dict] = []
        for link in links:
            if link.get("type") != "quiz" or link.get("position") in QUIZ_POSITIONS:
                kept.append(link)
                continue
            slot = next((pos for pos in QUIZ_SLOT_ORDER if pos not in occupied), None)
            if slot is None:
                notes.append(
                    f"{source}: dropped quiz {link['slug']} at {link['position']} (no free inline slot)"
                )
                continue
            moved = dict(link)
            moved["position"] = slot
            kept.append(moved)
            occupied.add(slot)
            notes.append(
                f"{source}: moved quiz {link['slug']} from {link['position']} to {slot}"
            )
        cleaned[source] = kept
    return cleaned, notes


def promote_100_to_related(
    mapping: dict[str, list[dict]],
) -> tuple[dict[str, list[dict]], list[str]]:
    """Turn every end-of-article (``100%``) link into a ``related`` item."""
    cleaned: dict[str, list[dict]] = {}
    notes: list[str] = []
    for source, links in mapping.items():
        promoted = 0
        new_links: list[dict] = []
        for link in links:
            if link.get("position") == "100%":
                link = dict(link)
                link["position"] = "related"
                promoted += 1
            new_links.append(link)
        if promoted:
            new_links.sort(key=lambda link: POSITION_ORDER.get(link["position"], 99))
            notes.append(f"{source}: moved {promoted} link(s) from 100% to related")
        cleaned[source] = new_links
    return cleaned, notes


def fix_incomplete_links(
    mapping: dict[str, list[dict]],
    inventory: list[InventoryItem],
) -> tuple[dict[str, list[dict]], list[str]]:
    """Repair links missing a key field, or drop the ones that cannot be repaired.

    A link needs ``slug``, ``text``, ``position`` and ``type``. The reviewer fills a
    missing ``type`` from the inventory when the slug is unambiguous, derives a missing
    anchor from the target's seoTitle, defaults a missing/unknown ``position`` to
    ``related``, and drops an entry with no slug, no resolvable type, or no anchor.
    """
    index = index_by_slug(inventory)
    cleaned: dict[str, list[dict]] = {}
    notes: list[str] = []
    for source, links in mapping.items():
        kept: list[dict] = []
        for number, link in enumerate(links, start=1):
            if not isinstance(link, dict):
                notes.append(f"{source}: dropped link #{number} (not an object)")
                continue
            slug = str(link.get("slug", "")).strip()
            if not slug:
                notes.append(f"{source}: dropped link #{number} (missing slug)")
                continue

            candidates = index.get(slug, [])
            type_ = str(link.get("type", "")).strip()
            if not type_:
                if len(candidates) == 1:
                    type_ = candidates[0].type
                    notes.append(f"{source}: filled missing type '{type_}' for {slug}")
                else:
                    notes.append(f"{source}: dropped {slug} (missing type)")
                    continue
            item = next((candidate for candidate in candidates if candidate.type == type_), None)
            if item is None and candidates:
                item = candidates[0]

            text = str(link.get("text", "")).strip()
            if not text:
                text = anchor_from_title(item.seo_title) if item is not None else ""
                if text:
                    notes.append(f"{source}: filled missing anchor for {slug} -> '{text}'")
                else:
                    notes.append(f"{source}: dropped {slug} (missing text)")
                    continue

            position = str(link.get("position", "")).strip()
            if position not in POSITIONS:
                notes.append(
                    f"{source}: defaulted position for {slug} to related "
                    f"(was '{position or 'missing'}')"
                )
                position = "related"

            fixed = dict(link)
            fixed.update({"slug": slug, "text": text, "position": position, "type": type_})
            kept.append(fixed)
        cleaned[source] = kept
    return cleaned, notes


def single_related_sources(
    mapping: dict[str, list[dict]], threshold: int = 1
) -> list[str]:
    """Sources whose Related Reading section holds exactly ``threshold`` items."""
    return [
        source
        for source, links in mapping.items()
        if sum(1 for link in links if link.get("position") == "related") == threshold
    ]


def fix_anchor_text(
    mapping: dict[str, list[dict]],
) -> tuple[dict[str, list[dict]], list[str]]:
    """Sentence-case every anchor and drop case-insensitive duplicates."""
    cleaned: dict[str, list[dict]] = {}
    notes: list[str] = []
    for source, links in mapping.items():
        seen: set[str] = set()
        kept: list[dict] = []
        for link in links:
            text = str(link.get("text", ""))
            fixed = capitalize_anchor(text)
            if fixed != text:
                link = dict(link)
                link["text"] = fixed
                notes.append(f"{source}: capitalized anchor '{text}' -> '{fixed}'")
            key = fixed.lower()
            if key and key in seen:
                notes.append(
                    f"{source}: dropped duplicate anchor '{fixed}' ({link.get('slug', '')})"
                )
                continue
            if key:
                seen.add(key)
            kept.append(link)
        cleaned[source] = kept
    return cleaned, notes


def review_mapping(
    mapping: dict[str, list[dict]],
    inventory: list[InventoryItem],
) -> tuple[dict[str, list[dict]], dict[str, list[str]]]:
    """Apply every deterministic repair, in order. Returns (mapping, notes)."""
    notes: dict[str, list[str]] = {}
    mapping, notes["incomplete"] = fix_incomplete_links(mapping, inventory)
    mapping, notes["anchors"] = fix_anchor_text(mapping)
    mapping, notes["reciprocal"] = fix_reciprocal_links(mapping, inventory)
    mapping, notes["quiz"] = fix_quiz_positions(mapping)
    mapping, notes["promoted"] = promote_100_to_related(mapping)
    return mapping, notes
