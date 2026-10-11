"""Pipeline test for the blog rating agent using a stub LLM (no network)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from contentos.agents.rater import load_rubric
from contentos.core import pipeline

PROMPT = Path(__file__).resolve().parents[1] / "prompts" / "examos-blog-rating.md"
RUBRIC = load_rubric(str(PROMPT))
SCORES = {criterion.key: 5 for criterion in RUBRIC.criteria}
PAYLOAD = {
    "scores": SCORES,
    "strengths": ["Great opening"],
    "weaknesses": ["One long paragraph"],
    "factual_issues": [],
    "summary": "Strong post.",
}


class FakeResponse:
    def __init__(self, content: str) -> None:
        self.content = content


class FakeLLM:
    def __init__(self, content: str) -> None:
        self.content = content

    def invoke(self, messages):
        return FakeResponse(self.content)


def _write_blog(tmp_path, slugs):
    blog_dir = tmp_path / "blog"
    blog_dir.mkdir()
    for slug in slugs:
        (blog_dir / f"{slug}.mdx").write_text(
            f"---\nslug: {slug}\ntitle: {slug}\n---\n\n# {slug}\n\nBody.\n", encoding="utf-8"
        )
    return blog_dir


def test_rate_blogs_writes_reports(tmp_path, monkeypatch):
    blog_dir = _write_blog(tmp_path, ["post-one", "post-two"])
    monkeypatch.setattr(pipeline, "gemini_llm", lambda **_kwargs: FakeLLM(json.dumps(PAYLOAD)))

    report = pipeline.run_rate_blogs(
        input_dir=str(blog_dir),
        reports_dir=str(tmp_path / "reports"),
        prompt_path=str(PROMPT),
        workers=2,
    )

    assert report.files_scanned == 2
    assert report.files_rated == 2
    assert report.average == 5.0
    assert report.grade_counts == {"A+": 2}

    markdown_path = report.extra["markdown_report"]
    json_path = report.extra["json_report"]
    markdown = Path(markdown_path).read_text(encoding="utf-8")
    assert markdown.count("# Rating:") == 2
    assert "**Grade:** A+" in markdown

    payload = json.loads(Path(json_path).read_text(encoding="utf-8"))
    assert payload["files_rated"] == 2
    assert payload["grade_counts"] == {"A+": 2}


def test_rate_blogs_slug_filter(tmp_path, monkeypatch):
    blog_dir = _write_blog(tmp_path, ["keep-me", "skip-me"])
    monkeypatch.setattr(pipeline, "gemini_llm", lambda **_kwargs: FakeLLM(json.dumps(PAYLOAD)))

    report = pipeline.run_rate_blogs(
        input_dir=str(blog_dir),
        reports_dir=str(tmp_path / "reports"),
        prompt_path=str(PROMPT),
        slug="keep-me",
        workers=1,
    )

    assert report.files_scanned == 1
    assert [record["slug"] for record in report.ratings] == ["keep-me"]


def test_rate_blogs_captures_per_file_failure(tmp_path, monkeypatch):
    blog_dir = _write_blog(tmp_path, ["bad-post"])
    monkeypatch.setattr(pipeline, "gemini_llm", lambda **_kwargs: FakeLLM("not json at all"))

    report = pipeline.run_rate_blogs(
        input_dir=str(blog_dir),
        reports_dir=str(tmp_path / "reports"),
        prompt_path=str(PROMPT),
        workers=1,
    )

    assert report.files_rated == 0
    assert len(report.errors) == 1
    assert report.errors[0]["slug"] == "bad-post"


def test_rate_blogs_aborts_on_auth_error(tmp_path, monkeypatch):
    blog_dir = _write_blog(tmp_path, ["post-one", "post-two"])

    class AuthError(Exception):
        status_code = 403

    class AuthLLM:
        def invoke(self, messages):
            raise AuthError("permission denied")

    monkeypatch.setattr(pipeline, "gemini_llm", lambda **_kwargs: AuthLLM())

    with pytest.raises(pipeline.FatalLLMError):
        pipeline.run_rate_blogs(
            input_dir=str(blog_dir),
            reports_dir=str(tmp_path / "reports"),
            prompt_path=str(PROMPT),
            workers=1,
        )


def test_rate_blogs_flushes_partial_report_on_interrupt(tmp_path, monkeypatch):
    blog_dir = _write_blog(tmp_path, ["post-one", "post-two"])
    calls = {"n": 0}

    class InterruptLLM:
        def invoke(self, messages):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise KeyboardInterrupt
            return FakeResponse(json.dumps(PAYLOAD))

    monkeypatch.setattr(pipeline, "gemini_llm", lambda **_kwargs: InterruptLLM())

    with pytest.raises(KeyboardInterrupt):
        pipeline.run_rate_blogs(
            input_dir=str(blog_dir),
            reports_dir=str(tmp_path / "reports"),
            prompt_path=str(PROMPT),
            workers=1,
        )

    reports = sorted((tmp_path / "reports").glob("rating-*.json"))
    assert reports, "a partial report should have been written before the interrupt"
    payload = json.loads(reports[-1].read_text(encoding="utf-8"))
    assert payload["files_rated"] == 1


def test_grounded_model_gets_the_search_timeout(tmp_path, monkeypatch):
    calls: list[dict] = []

    def fake_gemini(**kwargs):
        calls.append(kwargs)
        return FakeLLM(json.dumps(PAYLOAD))

    monkeypatch.setattr(pipeline, "gemini_llm", fake_gemini)
    blog_dir = _write_blog(tmp_path, ["post-one"])

    pipeline.run_rate_blogs(
        input_dir=str(blog_dir),
        reports_dir=str(tmp_path / "reports"),
        prompt_path=str(PROMPT),
        workers=1,
        timeout=11.0,
        search_timeout=222.0,
    )

    assert calls[0]["timeout"] == 11.0
    assert calls[0]["enable_search"] is False
    assert calls[1]["timeout"] == 222.0
    assert calls[1]["enable_search"] is True


def test_rate_blogs_caps_web_searches(tmp_path, monkeypatch):
    blog_dir = _write_blog(tmp_path, ["how-hard-is-ai-103", "why-cissp-is-hard"])
    monkeypatch.setattr(pipeline, "gemini_llm", lambda **_kwargs: FakeLLM(json.dumps(PAYLOAD)))

    report = pipeline.run_rate_blogs(
        input_dir=str(blog_dir),
        reports_dir=str(tmp_path / "reports"),
        prompt_path=str(PROMPT),
        workers=1,
        max_searches=1,
    )

    assert report.max_searches == 1
    assert report.searches_used == 1
    assert report.searches == 1
    exhausted = [
        warning
        for record in report.ratings
        for warning in record["warnings"]
        if "budget exhausted" in warning
    ]
    assert len(exhausted) == 1

    payload = json.loads(Path(report.extra["json_report"]).read_text(encoding="utf-8"))
    assert payload["max_web_searches"] == 1
    assert payload["search_calls"] == 1
    assert payload["web_searches"] == 1
