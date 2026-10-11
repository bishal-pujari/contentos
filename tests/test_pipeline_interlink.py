"""End-to-end interlink pipeline test using a stub LLM (no network)."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from contentos import cli
from contentos.core import pipeline


class FakeResponse:
    def __init__(self, content: str) -> None:
        self.content = content


class FakeLLM:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def invoke(self, messages):
        return FakeResponse(json.dumps(self.payload))


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
        {"slug": "b1", "text": "Misunderstood Concepts", "position": "50%", "type": "blog", "score": 60, "reason": "Core section"},
        {"slug": "b2", "text": "AI-103 Jobs and Salary", "position": "related", "type": "blog", "score": 60, "reason": "Same exam"},
        {"slug": "b3", "text": "How Hard Is AI-103", "position": "related", "type": "blog", "score": 60, "reason": "Same exam"},
        {"slug": "b4", "text": "Ready For AI-103", "position": "related", "type": "blog", "score": 60, "reason": "Same exam"},
    ],
}


def test_run_interlink_writes_mapping(tmp_path, monkeypatch):
    blog_dir = tmp_path / "blog"
    blog_dir.mkdir()
    (blog_dir / "ai-103-thing.mdx").write_text(
        "---\nslug: ai-103-thing\ntitle: AI-103 Thing\n---\n\n# AI-103 Thing\n\nBody.\n",
        encoding="utf-8",
    )

    inventory_path = tmp_path / "inventory.json"
    inventory_path.write_text(json.dumps(INVENTORY), encoding="utf-8")
    output_path = tmp_path / "output" / "interlinks.json"
    reports_dir = tmp_path / "reports"

    monkeypatch.setattr(pipeline, "gemini_llm", lambda **_kwargs: FakeLLM(PAYLOAD))

    report = pipeline.run_interlink(
        input_dir=str(blog_dir),
        inventory_path=str(inventory_path),
        output_path=str(output_path),
        reports_dir=str(reports_dir),
        apply=True,
    )

    assert report.files_scanned == 1
    assert report.files_processed == 1
    assert report.total_links == 5

    mapping = json.loads(output_path.read_text(encoding="utf-8"))
    assert "ai-103-thing" in mapping
    assert [item["position"] for item in mapping["ai-103-thing"]] == ["top", "50%", "related", "related", "related"]

    report_files = list(reports_dir.glob("interlink-*.json"))
    assert len(report_files) == 1
    payload = json.loads(report_files[0].read_text(encoding="utf-8"))
    assert payload["sources"][0]["primary_exam"] == "AI-103"
    assert payload["total_links"] == 5


def test_run_interlink_concurrent_workers(tmp_path, monkeypatch):
    blog_dir = tmp_path / "blog"
    blog_dir.mkdir()
    slugs = ["ai-103-one", "ai-103-two", "ai-103-three"]
    for slug in slugs:
        (blog_dir / f"{slug}.mdx").write_text(
            f"---\nslug: {slug}\n---\n\n# {slug}\n\nBody.\n", encoding="utf-8"
        )

    inventory_path = tmp_path / "inventory.json"
    inventory_path.write_text(json.dumps(INVENTORY), encoding="utf-8")
    output_path = tmp_path / "output" / "interlinks.json"

    monkeypatch.setattr(pipeline, "gemini_llm", lambda **_kwargs: FakeLLM(PAYLOAD))

    report = pipeline.run_interlink(
        input_dir=str(blog_dir),
        inventory_path=str(inventory_path),
        output_path=str(output_path),
        reports_dir=str(tmp_path / "reports"),
        apply=True,
        workers=3,
    )

    assert report.files_processed == 3
    assert report.total_links == 15
    mapping = json.loads(output_path.read_text(encoding="utf-8"))
    assert set(mapping) == set(slugs)
    # Sources keep discovery (sorted path) order even though work runs concurrently.
    assert [source.slug for source in report.sources] == sorted(slugs)


def test_run_interlink_output_directory_gets_default_file(tmp_path, monkeypatch):
    blog_dir = tmp_path / "blog"
    blog_dir.mkdir()
    (blog_dir / "ai-103-thing.mdx").write_text("---\nslug: ai-103-thing\n---\n\n# AI-103\n", encoding="utf-8")
    inventory_path = tmp_path / "inventory.json"
    inventory_path.write_text(json.dumps(INVENTORY), encoding="utf-8")
    out_dir = tmp_path / "output"
    out_dir.mkdir()

    monkeypatch.setattr(pipeline, "gemini_llm", lambda **_kwargs: FakeLLM(PAYLOAD))

    report = pipeline.run_interlink(
        input_dir=str(blog_dir),
        inventory_path=str(inventory_path),
        output_path=str(out_dir),
        reports_dir=str(tmp_path / "reports"),
        apply=True,
        workers=1,
    )

    assert report.files_processed == 1
    assert (out_dir / "interlinks.json").exists()
    assert report.extra["output"] == str(out_dir / "interlinks.json")


def test_run_interlink_reports_progress(tmp_path, monkeypatch):
    blog_dir = tmp_path / "blog"
    blog_dir.mkdir()
    for slug in ("ai-103-one", "ai-103-two"):
        (blog_dir / f"{slug}.mdx").write_text(f"---\nslug: {slug}\n---\n\n# {slug}\n", encoding="utf-8")
    inventory_path = tmp_path / "inventory.json"
    inventory_path.write_text(json.dumps(INVENTORY), encoding="utf-8")

    monkeypatch.setattr(pipeline, "gemini_llm", lambda **_kwargs: FakeLLM(PAYLOAD))

    seen: list[tuple[int, int, str]] = []
    started: list[str] = []
    pipeline.run_interlink(
        input_dir=str(blog_dir),
        inventory_path=str(inventory_path),
        reports_dir=str(tmp_path / "reports"),
        workers=2,
        on_progress=lambda index, count, outcome: seen.append((index, count, outcome.slug)),
        on_start=lambda index, count, slug: started.append(slug),
    )

    assert [item[0] for item in seen] == [1, 2]
    assert all(item[1] == 2 for item in seen)
    assert set(started) == {"ai-103-one", "ai-103-two"}


def test_run_interlink_dry_run_does_not_write(tmp_path, monkeypatch):
    blog_dir = tmp_path / "blog"
    blog_dir.mkdir()
    (blog_dir / "ai-103-thing.mdx").write_text("---\nslug: ai-103-thing\n---\n\n# AI-103\n", encoding="utf-8")
    inventory_path = tmp_path / "inventory.json"
    inventory_path.write_text(json.dumps(INVENTORY), encoding="utf-8")
    output_path = tmp_path / "output" / "interlinks.json"

    monkeypatch.setattr(pipeline, "gemini_llm", lambda **_kwargs: FakeLLM(PAYLOAD))

    report = pipeline.run_interlink(
        input_dir=str(blog_dir),
        inventory_path=str(inventory_path),
        output_path=str(output_path),
        reports_dir=str(tmp_path / "reports"),
        apply=False,
    )

    assert report.files_processed == 1
    assert not output_path.exists()


def test_run_interlink_falls_back_to_deepseek(tmp_path, monkeypatch):
    blog_dir = tmp_path / "blog"
    blog_dir.mkdir()
    (blog_dir / "ai-103-thing.mdx").write_text("---\nslug: ai-103-thing\n---\n\n# AI-103\n", encoding="utf-8")
    inventory_path = tmp_path / "inventory.json"
    inventory_path.write_text(json.dumps(INVENTORY), encoding="utf-8")

    class TimeoutLLM:
        def invoke(self, messages):
            raise type("APITimeoutError", (Exception,), {})()

    monkeypatch.setattr(pipeline, "gemini_llm", lambda **_kwargs: TimeoutLLM())
    monkeypatch.setattr(pipeline, "deepseek_available", lambda: True)
    monkeypatch.setattr(pipeline, "deepseek_llm", lambda **_kwargs: FakeLLM(PAYLOAD))

    switched: list[str] = []
    report = pipeline.run_interlink(
        input_dir=str(blog_dir),
        inventory_path=str(inventory_path),
        reports_dir=str(tmp_path / "reports"),
        workers=1,
        max_attempts=1,
        fallback=True,
        on_provider_switch=lambda provider, exc: switched.append(provider),
    )

    assert report.files_processed == 1
    assert report.total_links == 5
    assert switched == ["deepseek"]


def test_run_interlink_without_fallback_keeps_failure(tmp_path, monkeypatch):
    blog_dir = tmp_path / "blog"
    blog_dir.mkdir()
    (blog_dir / "ai-103-thing.mdx").write_text("---\nslug: ai-103-thing\n---\n\n# AI-103\n", encoding="utf-8")
    inventory_path = tmp_path / "inventory.json"
    inventory_path.write_text(json.dumps(INVENTORY), encoding="utf-8")

    class TimeoutLLM:
        def invoke(self, messages):
            raise type("APITimeoutError", (Exception,), {})()

    monkeypatch.setattr(pipeline, "gemini_llm", lambda **_kwargs: TimeoutLLM())
    monkeypatch.setattr(pipeline, "deepseek_available", lambda: True)
    monkeypatch.setattr(pipeline, "deepseek_llm", lambda **_kwargs: FakeLLM(PAYLOAD))

    report = pipeline.run_interlink(
        input_dir=str(blog_dir),
        inventory_path=str(inventory_path),
        reports_dir=str(tmp_path / "reports"),
        workers=1,
        max_attempts=1,
        fallback=False,
    )

    assert report.files_processed == 0
    assert report.sources[0].error


def test_interlink_cli_streams_progress(tmp_path, monkeypatch):
    blog_dir = tmp_path / "blog"
    blog_dir.mkdir()
    (blog_dir / "ai-103-thing.mdx").write_text("---\nslug: ai-103-thing\n---\n\n# AI-103\n", encoding="utf-8")
    inventory_path = tmp_path / "inventory.json"
    inventory_path.write_text(json.dumps(INVENTORY), encoding="utf-8")

    monkeypatch.setattr(pipeline, "gemini_llm", lambda **_kwargs: FakeLLM(PAYLOAD))

    result = CliRunner().invoke(
        cli.app,
        [
            "interlink",
            "--input",
            str(blog_dir),
            "--inventory",
            str(inventory_path),
            "--reports",
            str(tmp_path / "reports"),
            "--workers",
            "2",
            "--no-ping",
        ],
    )

    assert result.exit_code == 0
    assert "Starting interlink run" in result.output
    assert "ai-103-thing [AI-103] -> 5 links" in result.output
    assert "scanned=1" in result.output
