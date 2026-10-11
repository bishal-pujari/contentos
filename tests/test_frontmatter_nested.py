"""Phase 0 frontmatter fixes: nested YAML round-trips and nested_preserve."""

from __future__ import annotations

from pathlib import Path

import yaml

from contentos.agents.frontmatter import (
    _dump_yaml,
    _emit_unknown,
    _extract_raw_field,
    correct_frontmatter,
    load_frontmatter_schema,
)
from contentos.core.loaders import parse_document

PROMPT = Path(__file__).resolve().parents[1] / "prompts" / "examos-blog-frontmatter.md"
SCHEMA = load_frontmatter_schema(str(PROMPT))


def _doc(slug, frontmatter, body="# H1\n\nBody paragraph.\n"):
    return parse_document(f"---\n{frontmatter}\n---\n{body}", f"{slug}.mdx")


def test_dump_yaml_round_trips_nested_value():
    value = {
        "skills": [{"name": "python", "level": 2}],
        "milestones": ["start", "finish"],
    }
    emitted = _dump_yaml("timeline", value)
    assert emitted.startswith("timeline:\n  ")
    assert yaml.safe_load(emitted) == {"timeline": value}


def test_emit_unknown_keeps_scalar_lists_flow_style():
    assert _emit_unknown("tags", ["a", "b"]) == 'tags: ["a", "b"]'


def test_emit_unknown_dumps_list_of_dicts():
    emitted = _emit_unknown("steps", [{"title": "one"}, {"title": "two"}])
    assert yaml.safe_load(emitted) == {"steps": [{"title": "one"}, {"title": "two"}]}


def test_extract_raw_field_keeps_indentation():
    frontmatter = (
        'title: "T"\n'
        "skills:\n"
        "  - name: python\n"
        "    level: 2\n"
        'date: "2026-01-01"\n'
    )
    assert _extract_raw_field(frontmatter, "skills") == "skills:\n  - name: python\n    level: 2"


def test_unknown_nested_field_round_trips():
    doc = _doc(
        "post",
        'title: "A Title"\nslug: post\ntype: post\nimagePosition: top\n'
        'author: "Sarah Chen"\ncategories: ["cloud"]\ndate: "2026-01-01"\n'
        'seoTitle: "An SEO Title"\nseoDescription: "A description"\n'
        "milestones:\n  - title: start\n    done: true\n",
    )
    result = correct_frontmatter(doc, SCHEMA)
    parsed = yaml.safe_load(result.new_text.split("---")[1])
    assert parsed["milestones"] == [{"title": "start", "done": True}]
    assert "# H1" in result.new_text


def test_nested_preserve_emits_block_verbatim():
    frontmatter = (
        'title: "A Title"\nslug: post\ntype: post\nimagePosition: top\n'
        'author: "Sarah Chen"\ncategories: ["cloud"]\ndate: "2026-01-01"\n'
        'seoTitle: "An SEO Title"\nseoDescription: "A description"\n'
        "skills:\n  - name: python\n    level: 2\n"
    )
    doc = _doc("post", frontmatter)
    result = correct_frontmatter(doc, SCHEMA, nested_preserve=("skills",))
    assert "skills:\n  - name: python\n    level: 2" in result.new_text
    parsed = yaml.safe_load(result.new_text.split("---")[1])
    assert parsed["skills"] == [{"name": "python", "level": 2}]
