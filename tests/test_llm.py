"""LLM helper tests (no network)."""

from __future__ import annotations

import json

import pytest

from contentos.agents.interlinker import _invoke_json
from contentos.core import llm as llm_module
from contentos.core.llm import (
    SearchBudget,
    default_max_searches,
    gemini_available,
    gemini_backend,
    gemini_llm,
    gemini_model_name,
    message_text,
    vertex_credentials_available,
    vertex_location,
)


class Response:
    def __init__(self, content):
        self.content = content


class FakeLLM:
    def __init__(self, content):
        self.content = content

    def invoke(self, messages):
        return Response(self.content)


def test_message_text_handles_string():
    assert message_text(Response("hello")) == "hello"


def test_message_text_joins_content_blocks():
    blocks = [{"type": "text", "text": '{"a": '}, {"type": "text", "text": "1}"}]
    assert message_text(Response(blocks)) == '{"a": 1}'


def test_invoke_json_parses_content_blocks():
    payload = {"scores": {"x": 1}, "summary": "ok"}
    blocks = [{"type": "text", "text": json.dumps(payload)}]
    assert _invoke_json(FakeLLM(blocks), []) == payload


class SequenceLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def invoke(self, messages):
        self.calls.append(messages)
        return Response(self.responses.pop(0))


def test_invoke_json_retries_without_empty_assistant_turn():
    payload = {"ok": True}
    llm = SequenceLLM(["", json.dumps(payload)])
    assert _invoke_json(llm, []) == payload
    retry_messages = llm.calls[1]
    assert len(retry_messages) == 1
    assert retry_messages[0].content.startswith("Your previous response")


def test_invoke_json_echoes_nonempty_prior_text_on_retry():
    llm = SequenceLLM(["not json", json.dumps({"ok": 1})])
    assert _invoke_json(llm, []) == {"ok": 1}
    retry_messages = llm.calls[1]
    assert retry_messages[0].content == "not json"
    assert retry_messages[-1].content.startswith("Your previous response")


def test_gemini_backend_explicit(monkeypatch):
    monkeypatch.setenv("GEMINI_BACKEND", "vertex")
    assert gemini_backend() == "vertex"
    monkeypatch.setenv("GEMINI_BACKEND", "developer")
    assert gemini_backend() == "developer"


def test_gemini_backend_autodetects_project(monkeypatch):
    monkeypatch.delenv("GEMINI_BACKEND", raising=False)
    for name in ("VERTEX_PROJECT", "GOOGLE_CLOUD_PROJECT", "GCLOUD_PROJECT"):
        monkeypatch.delenv(name, raising=False)
    assert gemini_backend() == "developer"
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj")
    assert gemini_backend() == "vertex"


def test_vertex_location_default(monkeypatch):
    for name in ("VERTEX_LOCATION", "GOOGLE_CLOUD_REGION", "GOOGLE_CLOUD_LOCATION"):
        monkeypatch.delenv(name, raising=False)
    assert vertex_location() == "us-central1"


def test_vertex_location_strips_zone_suffix(monkeypatch):
    for name in ("VERTEX_LOCATION", "GOOGLE_CLOUD_LOCATION"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GOOGLE_CLOUD_REGION", "asia-south1-a")
    assert vertex_location() == "asia-south1"
    monkeypatch.setenv("GOOGLE_CLOUD_REGION", "us-central1")
    assert vertex_location() == "us-central1"


def test_gemini_model_defaults_per_backend(monkeypatch):
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    monkeypatch.setenv("GEMINI_BACKEND", "vertex")
    assert gemini_model_name() == "gemini-2.5-flash"
    monkeypatch.setenv("GEMINI_BACKEND", "developer")
    assert gemini_model_name() == "gemini-3.6-flash"


def test_vertex_requires_project(monkeypatch):
    monkeypatch.setenv("GEMINI_BACKEND", "vertex")
    for name in ("VERTEX_PROJECT", "GOOGLE_CLOUD_PROJECT", "GCLOUD_PROJECT"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match="GOOGLE_CLOUD_PROJECT"):
        gemini_llm()


def test_vertex_llm_constructs_with_project(monkeypatch):
    monkeypatch.setenv("GEMINI_BACKEND", "vertex")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash")
    assert gemini_llm() is not None


def test_vertex_credentials_explicit_file(monkeypatch, tmp_path):
    real = tmp_path / "sa.json"
    real.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(real))
    assert vertex_credentials_available() is True

    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(tmp_path / "missing.json"))
    assert vertex_credentials_available() is False


def test_gemini_available_vertex_requires_credentials(monkeypatch):
    monkeypatch.setenv("GEMINI_BACKEND", "vertex")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj")
    monkeypatch.setattr(llm_module, "vertex_credentials_available", lambda: False)
    assert gemini_available() is False
    monkeypatch.setattr(llm_module, "vertex_credentials_available", lambda: True)
    assert gemini_available() is True


def test_gemini_available_vertex_requires_project(monkeypatch):
    monkeypatch.setenv("GEMINI_BACKEND", "vertex")
    for name in ("VERTEX_PROJECT", "GOOGLE_CLOUD_PROJECT", "GCLOUD_PROJECT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(llm_module, "vertex_credentials_available", lambda: True)
    assert gemini_available() is False


def test_default_max_searches_env_override(monkeypatch):
    monkeypatch.delenv("MAX_WEB_SEARCHES", raising=False)
    assert default_max_searches() == 1500
    monkeypatch.setenv("MAX_WEB_SEARCHES", "42")
    assert default_max_searches() == 42
    monkeypatch.setenv("MAX_WEB_SEARCHES", "not-a-number")
    assert default_max_searches() == 1500


def test_search_budget_limits_calls():
    budget = SearchBudget(2)
    assert budget.limit == 2
    assert budget.used == 0
    assert budget.remaining == 2
    assert budget.try_acquire() is True
    assert budget.try_acquire() is True
    assert budget.try_acquire() is False
    assert budget.used == 2
    assert budget.remaining == 0


def test_search_budget_unlimited():
    budget = SearchBudget(0)
    assert budget.remaining is None
    for _ in range(10):
        assert budget.try_acquire() is True
    assert budget.used == 10
