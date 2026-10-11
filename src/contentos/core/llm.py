"""LLM connectivity.

Primary provider: Google Gemini, with optional Google Search grounding for
factual verification. Two backends are supported:

* **Vertex AI** (``aiplatform.googleapis.com``, via ``google-cloud-aiplatform``)
  — selected with ``GEMINI_BACKEND=vertex`` or a configured GCP project; bills to
  your Google Cloud project and authenticates with Application Default Credentials.
* **Gemini Developer API** — selected with ``GEMINI_BACKEND=developer`` and a
  ``GEMINI_API_KEY``.

Secondary provider: the DeepSeek API, used as a per-call fallback when a Gemini
call times out. The fallback is not sticky; DeepSeek is optional. Auth or
credential failures are fatal and abort the run.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
import warnings

from dotenv import load_dotenv

load_dotenv()

# google-genai logs a noisy, harmless AFC warning and langchain warns that newer
# Gemini models ignore temperature. Both clutter live progress; silence them.
logging.getLogger("google_genai.models").addFilter(
    lambda record: "automatic function calling" not in record.getMessage()
)
warnings.filterwarnings("ignore", message=r".*uses fixed sampling defaults.*")

DEVELOPER_DEFAULT_MODEL = "gemini-3.6-flash"
VERTEX_DEFAULT_MODEL = "gemini-2.5-flash"
DEEPSEEK_DEFAULT_MODEL = "deepseek-chat"
DEEPSEEK_DEFAULT_BASE_URL = "https://api.deepseek.com/v1"

# Per-run cap on Google Search grounded calls. The Developer API free tier allows
# roughly this many searches per day; on Vertex AI grounding is billed to the GCP
# project, so the cap is a self-imposed budget rather than a hard vendor limit.
DEFAULT_MAX_SEARCHES = 1500


def message_text(response) -> str:
    """Extract plain text from a chat response.

    LangChain chat models may return ``content`` as a string or as a list of
    content blocks (newer Gemini models do the latter). Join the text blocks so
    callers always get a string.
    """
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        if parts:
            return "".join(parts)
    return str(content)


def gemini_backend() -> str:
    """Return ``"vertex"`` or ``"developer"``.

    Explicit via ``GEMINI_BACKEND``; otherwise Vertex AI when a GCP project is
    configured, else the Gemini Developer API.
    """
    explicit = (os.environ.get("GEMINI_BACKEND") or "").strip().lower()
    if explicit in {"vertex", "vertexai", "aiplatform"}:
        return "vertex"
    if explicit in {"developer", "gemini", "api"}:
        return "developer"
    return "vertex" if vertex_project() else "developer"


def vertex_project() -> str:
    return (
        os.environ.get("VERTEX_PROJECT")
        or os.environ.get("GOOGLE_CLOUD_PROJECT")
        or os.environ.get("GCLOUD_PROJECT")
        or ""
    ).strip()


def vertex_location() -> str:
    """Vertex AI region.

    A gcloud *zone* such as ``asia-south1-a`` is silently reduced to its region
    (``asia-south1``) with a warning: zones are not valid Vertex endpoints and
    would otherwise produce a confusing ``404 ... invalid regional endpoint``.
    """
    raw = (
        os.environ.get("VERTEX_LOCATION")
        or os.environ.get("GOOGLE_CLOUD_REGION")
        or os.environ.get("GOOGLE_CLOUD_LOCATION")
        or "us-central1"
    ).strip()
    match = re.match(r"^(?P<region>.+)-[a-z]$", raw)
    if match:
        region = match.group("region")
        logging.getLogger(__name__).warning(
            "Vertex location %r looks like a zone; using region %r instead.", raw, region
        )
        return region
    return raw


def vertex_credentials_path() -> str:
    """Path from ``GOOGLE_APPLICATION_CREDENTIALS`` (empty when unset)."""
    return (os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or "").strip()


def vertex_credentials_available() -> bool:
    """True when Vertex AI can find credentials on this machine.

    Checks the explicit ``GOOGLE_APPLICATION_CREDENTIALS`` file first, then the
    well-known gcloud Application Default Credentials locations. This is a
    presence check only; it does not validate the credential.
    """
    explicit = vertex_credentials_path()
    if explicit:
        return os.path.isfile(explicit)

    candidates: list[str] = []
    cloudsdk_config = (os.environ.get("CLOUDSDK_CONFIG") or "").strip()
    if cloudsdk_config:
        candidates.append(os.path.join(cloudsdk_config, "application_default_credentials.json"))
    appdata = (os.environ.get("APPDATA") or "").strip()
    if appdata:
        candidates.append(os.path.join(appdata, "gcloud", "application_default_credentials.json"))
    candidates.append(
        os.path.join(
            os.path.expanduser("~"), ".config", "gcloud", "application_default_credentials.json"
        )
    )
    return any(os.path.isfile(path) for path in candidates)


def gemini_model_name() -> str:
    explicit = (os.environ.get("GEMINI_MODEL") or "").strip()
    if explicit:
        return explicit
    return VERTEX_DEFAULT_MODEL if gemini_backend() == "vertex" else DEVELOPER_DEFAULT_MODEL


def gemini_api_key() -> str:
    return (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()


def gemini_available() -> bool:
    """True when the selected Gemini backend is configured *and* usable.

    The Vertex backend needs both a project and discoverable credentials; without
    the latter any request fails, so a missing credential file reports ``False``.
    """
    if gemini_backend() == "vertex":
        return bool(vertex_project()) and vertex_credentials_available()
    return bool(gemini_api_key())


def default_max_searches() -> int:
    """Search-call budget for a run: ``MAX_WEB_SEARCHES`` env, else 1500."""
    raw = (os.environ.get("MAX_WEB_SEARCHES") or "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            return DEFAULT_MAX_SEARCHES
    return DEFAULT_MAX_SEARCHES


def deepseek_model_name() -> str:
    return os.environ.get("DEEPSEEK_MODEL", DEEPSEEK_DEFAULT_MODEL)


def deepseek_available() -> bool:
    """True when a DeepSeek API key is configured."""
    return bool((os.environ.get("DEEPSEEK_API_KEY") or "").strip())


def _chat_llm(*, api_key: str, model: str, base_url: str, timeout: float | None):
    try:
        from langchain_openai import ChatOpenAI
    except ImportError as err:  # pragma: no cover
        raise SystemExit(
            f"Could not import langchain-openai: {err}\n"
            'Install it with: pip install -e ".[dev]"'
        ) from err

    # Retries are handled explicitly by the agents so backoff and Retry-After
    # headers stay under our control (see agents/interlinker.py).
    return ChatOpenAI(
        api_key=api_key,
        model=model,
        base_url=base_url,
        temperature=0,
        max_retries=0,
        timeout=timeout,
    )


def gemini_llm(timeout: float | None = 120.0, *, enable_search: bool = False):
    """Return a Gemini chat model, optionally grounded with Google Search.

    Uses Vertex AI (``aiplatform.googleapis.com``, billed to your GCP project)
    when ``GEMINI_BACKEND=vertex`` or a GCP project is configured; otherwise the
    Gemini Developer API with an API key. ``enable_search`` binds the native
    ``google_search`` tool.
    """
    if gemini_backend() == "vertex":
        return _vertex_gemini_llm(timeout, enable_search)
    return _developer_gemini_llm(timeout, enable_search)


def _developer_gemini_llm(timeout: float | None, enable_search: bool):
    key = gemini_api_key()
    if not key:
        raise ValueError(
            "GEMINI_API_KEY is not set. Add a Google AI Studio key, or set "
            "GEMINI_BACKEND=vertex with GOOGLE_CLOUD_PROJECT to use Vertex AI."
        )
    try:
        from langchain_google_genai import ChatGoogleGenerativeAI
    except ImportError as err:  # pragma: no cover
        raise SystemExit(
            f"Could not import langchain-google-genai: {err}\n"
            'Install it with: pip install -e ".[dev]"'
        ) from err

    model = ChatGoogleGenerativeAI(
        api_key=key,
        model=gemini_model_name(),
        temperature=0,
        max_retries=0,
        timeout=timeout,
    )
    if enable_search:
        return model.bind_tools([{"google_search": {}}])
    return model


def _vertex_gemini_llm(timeout: float | None, enable_search: bool):
    """Vertex AI backend via the google-cloud-aiplatform SDK.

    Auth uses Application Default Credentials: run
    ``gcloud auth application-default login`` or point
    ``GOOGLE_APPLICATION_CREDENTIALS`` at a service-account JSON file.
    """
    project = vertex_project()
    if not project:
        raise ValueError(
            "GOOGLE_CLOUD_PROJECT (or VERTEX_PROJECT) is required for the Vertex AI backend."
        )
    if not vertex_credentials_available():
        raise ValueError(
            "No Google credentials found for Vertex AI. Run "
            "`gcloud auth application-default login` (then "
            "`gcloud auth application-default set-quota-project "
            f"{project}`), or set GOOGLE_APPLICATION_CREDENTIALS to a "
            "service-account JSON path."
        )
    try:
        from google.cloud.aiplatform_v1beta1.types import Tool as VertexTool
        from langchain_google_vertexai import ChatVertexAI
    except ImportError as err:  # pragma: no cover
        raise SystemExit(
            f"Could not import langchain-google-vertexai / google-cloud-aiplatform: {err}\n"
            'Install them with: pip install -e ".[dev]". If the error mentions a blocked '
            "DLL (Application Control policy), the native extension is blocked by the OS "
            "rather than missing."
        ) from err

    with warnings.catch_warnings():
        # ChatVertexAI carries a deprecation notice in favour of ChatGoogleGenerativeAI,
        # but it is the LangChain wrapper around the google-cloud-aiplatform SDK.
        warnings.simplefilter("ignore")
        model = ChatVertexAI(
            model=gemini_model_name(),
            project=project,
            location=vertex_location(),
            temperature=0,
            max_retries=0,
            timeout=timeout,
        )
    if enable_search:
        return model.bind_tools([VertexTool(google_search={})])
    return model


def deepseek_llm(timeout: float | None = 120.0):
    """Return a chat model pointed at the DeepSeek API.

    Requires DEEPSEEK_API_KEY to be set. Raises ValueError otherwise.
    """
    key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if not key:
        raise ValueError("DEEPSEEK_API_KEY is not set. Add it to .env to enable the fallback provider.")
    base_url = os.environ.get("DEEPSEEK_BASE_URL", DEEPSEEK_DEFAULT_BASE_URL)
    return _chat_llm(api_key=key, model=deepseek_model_name(), base_url=base_url, timeout=timeout)


def ping_llm(timeout: float = 30.0) -> str:
    """Send a trivial prompt to Gemini to verify key/model/endpoint.

    Returns the model's reply. Raises the underlying error on failure.
    """
    from langchain_core.messages import HumanMessage

    response = gemini_llm(timeout=timeout).invoke(
        [HumanMessage(content="Reply with the single word: OK")]
    )
    return message_text(response).strip()


class RateLimiter:
    """Thread-safe limiter that spaces out request starts.

    ``min_interval`` is the minimum number of seconds between two requests.
    A value of 0 disables throttling. Safe to share across worker threads.
    """

    def __init__(self, min_interval: float = 0.0) -> None:
        self.min_interval = max(0.0, min_interval)
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def wait(self) -> None:
        if self.min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            sleep_for = max(0.0, self._next_allowed - now)
            self._next_allowed = max(now, self._next_allowed) + self.min_interval
        if sleep_for > 0:
            time.sleep(sleep_for)


class RateLimitedLLM:
    """Wrap a chat model so every ``invoke`` waits for a shared rate limiter."""

    def __init__(self, llm, limiter: RateLimiter) -> None:
        self._llm = llm
        self._limiter = limiter

    def invoke(self, messages):
        self._limiter.wait()
        return self._llm.invoke(messages)


class SearchBudget:
    """Thread-safe per-run cap on Google Search grounded calls.

    ``limit`` <= 0 means unlimited. ``try_acquire`` atomically reserves one call
    and returns ``False`` once the budget is spent, so concurrent workers share a
    single allowance.
    """

    def __init__(self, limit: int = 0) -> None:
        self.limit = int(limit)
        self._lock = threading.Lock()
        self._used = 0

    @property
    def used(self) -> int:
        with self._lock:
            return self._used

    @property
    def remaining(self) -> int | None:
        """Calls left, or ``None`` when the budget is unlimited."""
        if self.limit <= 0:
            return None
        with self._lock:
            return max(0, self.limit - self._used)

    def try_acquire(self) -> bool:
        with self._lock:
            if self.limit > 0 and self._used >= self.limit:
                return False
            self._used += 1
            return True


def status_code(exc: Exception) -> int | None:
    """Best-effort HTTP status code from an SDK exception (duck-typed)."""
    code = getattr(exc, "status_code", None)
    if isinstance(code, int):
        return code
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    if isinstance(code, int):
        return code
    # google.api_core / google-cloud exceptions expose the HTTP-equivalent code
    # (e.g. DeadlineExceeded.code == 504, PermissionDenied.code == 403).
    code = getattr(exc, "code", None)
    return code if isinstance(code, int) else None


# Errors that mean the credentials/endpoint are wrong rather than the request
# being momentarily unlucky. Retrying or falling back cannot help.
AUTH_STATUS_CODES = {401, 403}
AUTH_NAME_TOKENS = (
    "unauthenticated",
    "permissiondenied",
    "defaultcredentials",
    "refresherror",
    "invalidapikey",
)
AUTH_MESSAGE_TOKENS = (
    "api key not valid",
    "api_key_invalid",
    "invalid api key",
    "permission denied",
    "unauthenticated",
    "default credentials",
    "could not automatically determine credentials",
)


def is_auth_error(exc: Exception) -> bool:
    """True for authentication/authorization failures (fatal, never retried)."""
    if status_code(exc) in AUTH_STATUS_CODES:
        return True
    name = type(exc).__name__.lower()
    if any(token in name for token in AUTH_NAME_TOKENS):
        return True
    message = str(exc).lower()
    return any(token in message for token in AUTH_MESSAGE_TOKENS)


class FatalLLMError(RuntimeError):
    """Raised when the LLM is misconfigured (e.g. bad credentials).

    Unlike a per-source failure, this aborts the whole run: every later call
    would fail the same way, so continuing only wastes time and quota.
    """


def is_timeout_error(exc: Exception) -> bool:
    """True for request timeouts / dropped connections (the fallback trigger)."""
    name = type(exc).__name__.lower()
    return any(token in name for token in ("timeout", "connection", "deadline"))


class FallbackLLM:
    """Use a secondary provider for a single call when the primary times out.

    Not sticky: every call tries the primary first, so one transient Gemini
    timeout only diverts that request — the next call starts with Gemini again.
    Safe to share across worker threads. An auth error never falls back; it
    raises :class:`FatalLLMError` so the run stops instead of hammering a bad
    credential.
    """

    def __init__(self, primary, fallback, on_switch=None, is_fallback_error=is_timeout_error) -> None:
        self._primary = primary
        self._fallback = fallback
        self._on_switch = on_switch
        self._is_fallback_error = is_fallback_error
        self._local = threading.local()

    @property
    def used_fallback(self) -> bool:
        """True when the most recent ``invoke`` on this thread used the fallback.

        Thread-local, so concurrent workers never see each other's flag. Lets a caller
        that needs grounding (e.g. the rater's Google Search pass) tell whether the answer
        actually came from the primary provider or was downgraded.
        """
        return bool(getattr(self._local, "used_fallback", False))

    def invoke(self, messages):
        self._local.used_fallback = False
        try:
            return self._primary.invoke(messages)
        except Exception as exc:
            if is_auth_error(exc):
                raise FatalLLMError(f"{type(exc).__name__}: {exc}") from exc
            if not self._is_fallback_error(exc):
                raise
            if self._on_switch:
                self._on_switch(exc)
            self._local.used_fallback = True
            return self._fallback.invoke(messages)
