"""Tests for LLM retry/backoff and rate limiting (no network)."""

from __future__ import annotations

import pytest

from contentos.agents import interlinker
from contentos.core.llm import (
    FallbackLLM,
    FatalLLMError,
    RateLimitedLLM,
    RateLimiter,
    deepseek_available,
    is_auth_error,
    is_timeout_error,
    status_code,
)


class APITimeoutError(Exception):
    pass


class APIConnectionError(Exception):
    pass


class FakeResponse:
    def __init__(self, content: str = '{"ok": true}') -> None:
        self.content = content


class FakeStatusError(Exception):
    def __init__(self, status_code: int, retry_after: str | None = None) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code
        self.response = type("Resp", (), {"status_code": status_code, "headers": {}})()
        if retry_after is not None:
            self.response.headers["retry-after"] = retry_after


class FlakyLLM:
    def __init__(self, failures: int, error: Exception | None = None) -> None:
        self.failures = failures
        self.error = error or FakeStatusError(429)
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error
        return FakeResponse()


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(interlinker.time, "sleep", lambda _seconds: None)


def test_is_retryable_status_codes():
    assert interlinker.is_retryable(FakeStatusError(429))
    assert interlinker.is_retryable(FakeStatusError(500))
    assert not interlinker.is_retryable(FakeStatusError(404))


def test_is_retryable_by_exception_name():
    assert interlinker.is_retryable(type("APITimeoutError", (Exception,), {})())
    assert interlinker.is_retryable(type("RateLimitError", (Exception,), {})())


def test_retry_after_seconds_parsed():
    assert interlinker._retry_after_seconds(FakeStatusError(429, retry_after="7")) == 7.0
    assert interlinker._retry_after_seconds(FakeStatusError(429)) is None


def test_invoke_with_retry_eventually_succeeds():
    llm = FlakyLLM(failures=2)
    retries: list[tuple] = []
    response = interlinker._invoke_with_retry(
        llm, ["msg"], max_attempts=5, on_retry=lambda *args: retries.append(args)
    )
    assert response.content == '{"ok": true}'
    assert llm.calls == 3
    assert len(retries) == 2
    assert retries[0][0] == 1 and retries[0][1] == 5  # attempt, max_attempts


def test_invoke_with_retry_gives_up():
    llm = FlakyLLM(failures=99)
    with pytest.raises(FakeStatusError):
        interlinker._invoke_with_retry(llm, ["msg"], max_attempts=3)
    assert llm.calls == 3


def test_invoke_with_retry_does_not_retry_client_errors():
    llm = FlakyLLM(failures=1, error=FakeStatusError(404))
    with pytest.raises(FakeStatusError):
        interlinker._invoke_with_retry(llm, ["msg"], max_attempts=5)
    assert llm.calls == 1


def test_rate_limiter_disabled_is_noop():
    limiter = RateLimiter(0.0)
    limiter.wait()
    limiter.wait()


def test_rate_limited_llm_delegates():
    calls = []

    class RecordingLLM:
        def invoke(self, messages):
            calls.append(messages)
            return FakeResponse()

    wrapped = RateLimitedLLM(RecordingLLM(), RateLimiter(0.0))
    wrapped.invoke(["a"])
    wrapped.invoke(["b"])
    assert len(calls) == 2


def test_is_timeout_error():
    assert is_timeout_error(APITimeoutError())
    assert is_timeout_error(APIConnectionError())
    assert not is_timeout_error(FakeStatusError(429))


class GoogleApiError(Exception):
    """Mimics google.api_core exceptions: HTTP-equivalent ``code`` attribute."""

    def __init__(self, code: int) -> None:
        super().__init__(f"HTTP {code}")
        self.code = code


def test_deadline_exceeded_is_timeout_and_retryable():
    deadline = type("DeadlineExceeded", (GoogleApiError,), {})(504)
    assert status_code(deadline) == 504
    assert is_timeout_error(deadline)
    assert interlinker.is_retryable(deadline)


def test_auth_detected_from_google_code_attribute():
    assert status_code(GoogleApiError(403)) == 403
    assert is_auth_error(GoogleApiError(401))
    assert is_auth_error(GoogleApiError(403))
    assert not is_auth_error(GoogleApiError(504))


def test_fallback_switches_on_deadline_exceeded():
    class Primary:
        def invoke(self, messages):
            raise type("DeadlineExceeded", (GoogleApiError,), {})(504)

    class Fallback:
        def invoke(self, messages):
            return FakeResponse('{"from": "fallback"}')

    llm = FallbackLLM(Primary(), Fallback())
    assert llm.invoke(["a"]).content == '{"from": "fallback"}'


def test_fallback_switches_per_call_and_is_not_sticky():
    class FlakyPrimary:
        def __init__(self):
            self.calls = 0

        def invoke(self, messages):
            self.calls += 1
            if self.calls == 1:
                raise APITimeoutError()
            return FakeResponse('{"from": "primary"}')

    class Fallback:
        def __init__(self):
            self.calls = 0

        def invoke(self, messages):
            self.calls += 1
            return FakeResponse('{"from": "fallback"}')

    primary, fallback = FlakyPrimary(), Fallback()
    switches: list[Exception] = []
    llm = FallbackLLM(primary, fallback, on_switch=switches.append)

    assert llm.invoke(["a"]).content == '{"from": "fallback"}'  # timeout -> fallback
    assert llm.invoke(["b"]).content == '{"from": "primary"}'  # next call tries Gemini again

    assert primary.calls == 2
    assert fallback.calls == 1
    assert len(switches) == 1


def test_fallback_used_flag_reflects_the_last_call():
    class Primary:
        def __init__(self):
            self.fail = False

        def invoke(self, messages):
            if self.fail:
                raise APITimeoutError()
            return FakeResponse('{"from": "primary"}')

    class Fallback:
        def invoke(self, messages):
            return FakeResponse('{"from": "fallback"}')

    primary = Primary()
    llm = FallbackLLM(primary, Fallback())

    assert llm.used_fallback is False
    llm.invoke(["a"])
    assert llm.used_fallback is False  # primary answered

    primary.fail = True
    llm.invoke(["b"])
    assert llm.used_fallback is True  # timed out -> fallback

    primary.fail = False
    llm.invoke(["c"])
    assert llm.used_fallback is False  # not sticky


def test_fallback_does_not_switch_on_non_timeout():
    class Primary:
        def invoke(self, messages):
            raise FakeStatusError(429)

    class Fallback:
        def invoke(self, messages):
            raise AssertionError("fallback must not be used")

    llm = FallbackLLM(Primary(), Fallback())
    with pytest.raises(FakeStatusError):
        llm.invoke(["a"])


def test_fallback_never_falls_back_on_auth():
    class Primary:
        def invoke(self, messages):
            raise FakeStatusError(403)

    class Fallback:
        def invoke(self, messages):
            raise AssertionError("fallback must not be used for auth errors")

    llm = FallbackLLM(Primary(), Fallback())
    with pytest.raises(FatalLLMError):
        llm.invoke(["a"])


def test_invoke_with_retry_raises_fatal_on_auth():
    llm = FlakyLLM(failures=1, error=FakeStatusError(401))
    with pytest.raises(FatalLLMError):
        interlinker._invoke_with_retry(llm, ["msg"], max_attempts=5)
    assert llm.calls == 1


def test_invoke_with_retry_raises_fatal_on_credentials_error():
    creds_error = type(
        "DefaultCredentialsError", (Exception,), {"__str__": lambda self: "no ADC"}
    )()
    llm = FlakyLLM(failures=1, error=creds_error)
    with pytest.raises(FatalLLMError):
        interlinker._invoke_with_retry(llm, ["msg"], max_attempts=5)
    assert llm.calls == 1


def test_deepseek_available(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    assert not deepseek_available()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    assert deepseek_available()
