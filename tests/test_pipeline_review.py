"""Pipeline test for the deterministic reviewer + LLM redo (no network)."""

from __future__ import annotations

import json

from contentos.core import pipeline

INVENTORY = [
    {"type": "study-plan", "slug": "sp", "seoTitle": "AI-103 Study Plan | ExamOS", "seoDescription": "Plan for AI-103"},
    {"type": "blog", "slug": "b1", "seoTitle": "AI-103 Misunderstood Concepts", "seoDescription": "AI-103 concepts"},
    {"type": "blog", "slug": "b2", "seoTitle": "AI-103 Jobs and Salary", "seoDescription": "AI-103 careers"},
    {"type": "blog", "slug": "b3", "seoTitle": "How Hard Is AI-103", "seoDescription": "AI-103 difficulty"},
    {"type": "blog", "slug": "b4", "seoTitle": "Am I Ready For AI-103", "seoDescription": "AI-103 readiness"},
]

PAYLOAD = {
    "primaryExam": "AI-103",
    "primaryTopic": "Azure AI engineering",
    "audience": "Azure developers",
    "noExamFound": False,
    "links": [
        {"slug": "sp", "text": "AI-103 Study Plan", "position": "top", "type": "study-plan", "score": 100, "reason": "Intro"},
        {"slug": "b1", "text": "Misunderstood Concepts", "position": "50%", "type": "blog", "score": 60, "reason": "Core"},
        {"slug": "b2", "text": "AI-103 Jobs", "position": "related", "type": "blog", "score": 60, "reason": "Same exam"},
        {"slug": "b3", "text": "How Hard Is AI-103", "position": "related", "type": "blog", "score": 60, "reason": "Same exam"},
        {"slug": "b4", "text": "Ready For AI-103", "position": "related", "type": "blog", "score": 60, "reason": "Same exam"},
    ],
}

EXISTING = {
    "why-x": [
        {"slug": "sp", "text": "AI-103 Study Plan", "position": "top", "type": "study-plan", "reason": "Intro"},
        {"slug": "b1", "text": "Old End Link", "position": "100%", "type": "blog", "reason": "Old"},
        {"slug": "b2", "text": "Old Related", "position": "related", "type": "blog", "reason": "Old"},
    ],
    "other-source": [
        {"slug": "b3", "text": "Other", "position": "related", "type": "blog", "reason": "Untouched"},
        {"slug": "b4", "text": "Other Two", "position": "related", "type": "blog", "reason": "Untouched"},
    ],
}


class FakeResponse:
    def __init__(self, content: str) -> None:
        self.content = content


class FakeLLM:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def invoke(self, messages):
        return FakeResponse(json.dumps(self.payload))


def _setup(tmp_path):
    blog_dir = tmp_path / "blog"
    blog_dir.mkdir()
    (blog_dir / "why-x.mdx").write_text(
        "---\nslug: why-x\ntitle: Why X\n---\n\n# Why X\n\nBody.\n", encoding="utf-8"
    )
    inventory_path = tmp_path / "inventory.json"
    inventory_path.write_text(json.dumps(INVENTORY), encoding="utf-8")
    mapping_path = tmp_path / "output" / "interlinks.json"
    mapping_path.parent.mkdir(parents=True, exist_ok=True)
    mapping_path.write_text(json.dumps(EXISTING), encoding="utf-8")
    return blog_dir, inventory_path, mapping_path


def test_review_redoes_single_related_source(tmp_path, monkeypatch):
    blog_dir, inventory_path, mapping_path = _setup(tmp_path)
    monkeypatch.setattr(pipeline, "gemini_llm", lambda **_kwargs: FakeLLM(PAYLOAD))

    report = pipeline.run_review_interlinks(
        mapping_path=str(mapping_path),
        input_dir=str(blog_dir),
        inventory_path=str(inventory_path),
        reports_dir=str(tmp_path / "reports"),
        apply=True,
        workers=1,
    )

    assert report.redo_sources == ["why-x"]
    assert [source.slug for source in report.redo_results] == ["why-x"]
    assert report.redo_results[0].error is None

    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    assert "other-source" in mapping
    assert [item["position"] for item in mapping["why-x"]] == ["top", "50%", "related", "related", "related"]
    assert all(item["position"] != "100%" for item in mapping["why-x"])

    report_files = list((tmp_path / "reports").glob("review-*.json"))
    assert len(report_files) == 1


def test_review_dry_run_does_not_write(tmp_path, monkeypatch):
    blog_dir, inventory_path, mapping_path = _setup(tmp_path)
    monkeypatch.setattr(pipeline, "gemini_llm", lambda **_kwargs: FakeLLM(PAYLOAD))
    before = mapping_path.read_text(encoding="utf-8")

    report = pipeline.run_review_interlinks(
        mapping_path=str(mapping_path),
        input_dir=str(blog_dir),
        inventory_path=str(inventory_path),
        reports_dir=str(tmp_path / "reports"),
        apply=False,
        workers=1,
    )

    assert report.redo_sources == ["why-x"]
    assert mapping_path.read_text(encoding="utf-8") == before


def test_review_no_redo_only_applies_deterministic_fixes(tmp_path):
    blog_dir, inventory_path, mapping_path = _setup(tmp_path)

    report = pipeline.run_review_interlinks(
        mapping_path=str(mapping_path),
        input_dir=str(blog_dir),
        inventory_path=str(inventory_path),
        reports_dir=str(tmp_path / "reports"),
        apply=True,
        redo=False,
    )

    assert report.redo_sources == []
    assert any("100% to related" in note for note in report.promoted_to_related)
    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    assert all(item["position"] != "100%" for item in mapping["why-x"])
    assert [item["position"] for item in mapping["why-x"]] == ["top", "related", "related"]
