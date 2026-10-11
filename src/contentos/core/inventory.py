"""Content inventory loading and lookup helpers.

The inventory is a JSON array of every linkable target on the site. Each entry
has a ``type`` (blog / exam / quiz / roadmap / study-plan), a ``slug``, an
``seoTitle`` and an ``seoDescription``. Quizzes share their slug with the exam
they practise, so lookups are keyed by ``(slug, type)``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

EXAM_CODE_RE = re.compile(r"\b([A-Z]{2,5})[- ]?(\d{2,4})\b")


@dataclass(frozen=True)
class InventoryItem:
    type: str
    slug: str
    seo_title: str
    seo_description: str

    @property
    def key(self) -> tuple[str, str]:
        return (self.slug, self.type)

    def to_prompt_dict(self) -> dict[str, str]:
        return {
            "type": self.type,
            "slug": self.slug,
            "seoTitle": self.seo_title,
            "seoDescription": self.seo_description,
        }


def load_inventory(path: str) -> list[InventoryItem]:
    """Load and normalise a content inventory JSON file."""
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, list):
        raise TypeError(f"Inventory must be a JSON array: {path}")

    items: list[InventoryItem] = []
    for entry in raw:
        items.append(
            InventoryItem(
                type=str(entry.get("type", "")).strip(),
                slug=str(entry.get("slug", "")).strip(),
                seo_title=str(entry.get("seoTitle", "")).strip(),
                seo_description=str(entry.get("seoDescription", "")).strip(),
            )
        )
    return items


def index_by_slug(items: list[InventoryItem]) -> dict[str, list[InventoryItem]]:
    index: dict[str, list[InventoryItem]] = {}
    for item in items:
        index.setdefault(item.slug, []).append(item)
    return index


def find(items: list[InventoryItem], slug: str, type_: str) -> InventoryItem | None:
    for item in items:
        if item.slug == slug and item.type == type_:
            return item
    return None


def exam_codes(text: str) -> set[str]:
    """Extract normalised exam codes (e.g. AI-103, SAA-C03) from free text."""
    return {f"{prefix}-{digits}".upper() for prefix, digits in EXAM_CODE_RE.findall(text or "")}


def item_matches_code(item: InventoryItem, code: str) -> bool:
    """True when an inventory item refers to the given exam code."""
    needle = (code or "").strip().lower()
    if not needle:
        return False
    haystack = f"{item.slug} {item.seo_title} {item.seo_description}".lower()
    return needle in haystack
