"""Agent 2: interlink — choose internal links for a source article.

The LLM reads one source document plus the full content inventory and returns a
richer analysis object (primary exam, topic, audience, scored candidate links).
Python then enforces the hard rules before the mapping is accepted:

* nothing scoring below 40 is linked;
* at most one link per inline position (20% / 50% / 75%); an end-of-article
  ``100%`` link is promoted to ``related``;
* ``top`` is required and may never be an exam or quiz;
* ``related`` is required (3-5 preferred) and appended at the bottom;
* quizzes only ever point at the source's primary exam, at 50% or 75%;
* anchor text is 3-7 words, has no banned phrases / em dashes / exclamation marks
  and is never duplicated;
* the source never links to itself.

The LLM only *selects*; the rules above are executed in code so a bad model
response can never break the output contract.
"""

from __future__ import annotations

import json
import os
import random
import re
import time
from dataclasses import dataclass, field

from contentos.core.inventory import InventoryItem, find, item_matches_code
from contentos.core.llm import FatalLLMError, is_auth_error, message_text, status_code

INLINE_POSITIONS = ("20%", "50%", "75%", "100%")
POSITIONS = ("top", *INLINE_POSITIONS, "related")
POSITION_ORDER = {pos: i for i, pos in enumerate(POSITIONS)}
FOUNDATIONAL_TYPES = ("study-plan", "roadmap")
# The three real inline slots (100% is coerced to related before this stage).
QUIZ_POSITIONS = ("20%", "50%", "75%")
# Quizzes prefer the middle of the article; the 20% slot is the last choice.
QUIZ_SLOT_ORDER = ("50%", "75%", "20%")
MAX_ANCHOR_WORDS = 7
MIN_SCORE = 40
TOP_MIN_SCORE = 80
MAX_RELATED = 9
MIN_RELATED = 3
MAX_QUIZZES = 3
BANNED_ANCHOR_PHRASES = ("click here", "read more", "learn more")

# Transient failures we retry: rate limits, request conflicts/timeouts and 5xx.
RETRY_STATUS_CODES = {408, 409, 429}
RETRY_BASE_DELAY = 2.0
RETRY_MAX_DELAY = 60.0
DEFAULT_MAX_ATTEMPTS = 5

SLUG_RE = re.compile(r"^\s*slug:\s*[\"']?([^\"'\n]+?)[\"']?\s*$", re.MULTILINE)


@dataclass
class InterlinkAnalysis:
    """Raw LLM output for a single source, before guardrails."""

    source_slug: str
    primary_exam: str | None = None
    primary_topic: str = ""
    audience: str = ""
    no_exam_found: bool = False
    links: list[dict] = field(default_factory=list)


def source_slug(doc) -> str:
    """Frontmatter ``slug`` if present, otherwise the file stem."""
    match = SLUG_RE.search(doc.frontmatter or "")
    if match:
        return match.group(1).strip()
    return os.path.splitext(os.path.basename(doc.path))[0]


def frontmatter_exam_codes(frontmatter: str) -> list[str]:
    """The source's ``examCodes`` frontmatter list (empty when absent)."""
    match = re.search(r"(?m)^examCodes:\s*(.*)$", frontmatter or "")
    if not match:
        return []
    raw = match.group(1).strip()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    return [item.strip().strip("\"'") for item in raw.split(",") if item.strip()]


# --- Prompt -----------------------------------------------------------------

SYSTEM_PROMPT = """You are the interlink agent for ExamOS, an IT-certification study site.
You read ONE source article and the full content inventory, then choose internal links that
help readers and search engines.

1. ANALYSE THE SOURCE
- Identify the source's exam codes from its frontmatter `examCodes` (they are authoritative).
  The first code is the primary exam. If there is no examCodes list, look in the slug, title,
  seoTitle, headings and body. If the article is not about any specific exam, set primaryExam
  to null and noExamFound to true.
- Identify the primary topic and the target audience.

2. SCORE EVERY INVENTORY ITEM for relevance to the source:
100 exact exam (same exam code in source and item)
80 prerequisite or successor (known prior/next step for the source exam)
60 same certification family (same vendor and same track)
40 same primary topic
20 adjacent topic
0 unrelated
Only items scoring 40 or above may be linked. Choose the highest-scoring item that fits each position.

3. QUIZZES AND EXAMS (driven by examCodes)
- Quizzes and exam pages share a slug with their exam. For EVERY exam code in the source's
  examCodes list, prefer that exam's quiz as an inline link (one quiz per code, up to three).
  At minimum, the first exam code's quiz must be linked.
- Place quizzes at an inline position: 20%, 50% or 75%. Prefer 50% then 75% then 20%.
- The exam page is lower priority than the quiz: put the exam link in the related section
  (never use the exam in place of the required quiz).
- Never link both the quiz and the exam for the same exam code at the same position.

4. POSITIONS (choose one per link)
top   After the intro, before the first H2. REQUIRED. Score 80+. Must be foundational:
      a study plan or roadmap for the source's primary exam when one exists, otherwise a
      beginner guide. NEVER an exam or quiz.
20%   ~one-fifth through the body (first major section). Optional.
50%   ~middle (third or fourth major section). Optional.
75%   ~three-quarters (second-to-last major section). Optional.
      The three inline slots are a mix of quizzes (required above), an exam, or other blogs.
related  Appended Related Reading section. REQUIRED. 3-9 items scoring 40 or above; include
      the source's exam page(s) here. Use `related` for any end-of-article item; NEVER use a
      "100%" position.
- At most one link per inline position (20/50/75).
- For every link, name the section heading or quote a short sentence in `reason` to justify the spot.
- Never link the source article to itself.

5. ANCHOR TEXT (`text`)
- Derive a meaningful 3-7 word phrase from the target's seoTitle. Never exceed 7 words.
- Start with a capital letter (sentence case): "Why Cramming Fails", not "why cramming fails".
- No "click here", "read more" or "learn more".
- No em dashes and no exclamation marks.
- Do not repeat the same anchor text.

OUTPUT
Return ONLY a JSON object. No markdown, no prose, no code fence:
{
  "primaryExam": "AI-103" or null,
  "primaryTopic": "short phrase",
  "audience": "short phrase",
  "noExamFound": false,
  "links": [
    {
      "slug": "target-slug",
      "text": "Anchor text",
      "position": "top|20%|50%|75%|related",
      "type": "blog|exam|quiz|roadmap|study-plan",
      "score": 0,
      "reason": "Section heading or short quote that justifies the position."
    }
  ]
}"""


def build_user_prompt(source_slug: str, doc, inventory: list[InventoryItem]) -> str:
    inventory_json = json.dumps(
        [item.to_prompt_dict() for item in inventory], ensure_ascii=False
    )
    return (
        f"SOURCE SLUG: {source_slug}\n\n"
        f"FRONTMATTER:\n{doc.frontmatter or '(none)'}\n\n"
        f"SOURCE BODY:\n{doc.body}\n\n"
        f"CONTENT INVENTORY (JSON array):\n{inventory_json}\n\n"
        "Return only the JSON object described in the system instructions."
    )


def _extract_json(text: str) -> dict:
    if not text or not text.strip():
        raise ValueError("empty LLM response")
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9]*\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("no JSON object found in LLM response")
    return json.loads(text[start : end + 1])


def _retry_after_seconds(exc: Exception) -> float | None:
    """Read a ``Retry-After`` header (seconds) if the server sent one."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or getattr(exc, "headers", None)
    if not headers:
        return None
    try:
        value = headers.get("retry-after") or headers.get("Retry-After")
    except (AttributeError, TypeError):
        return None
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


def is_retryable(exc: Exception) -> bool:
    """True for rate limits, conflicts/timeouts and server errors."""
    code = status_code(exc)
    if code is not None:
        return code in RETRY_STATUS_CODES or code >= 500
    name = type(exc).__name__.lower()
    return any(token in name for token in ("timeout", "connection", "ratelimit"))


def _invoke_with_retry(llm, messages: list, max_attempts: int, on_retry=None):
    """Invoke the model, retrying transient failures with exponential backoff.

    Auth/credential failures are fatal: they raise :class:`FatalLLMError`
    immediately so the whole run stops rather than retrying a bad key.
    """
    delay = RETRY_BASE_DELAY
    for attempt in range(1, max_attempts + 1):
        try:
            return llm.invoke(messages)
        except FatalLLMError:
            raise
        except Exception as exc:
            if is_auth_error(exc):
                raise FatalLLMError(f"{type(exc).__name__}: {exc}") from exc
            if attempt >= max_attempts or not is_retryable(exc):
                raise
            wait = _retry_after_seconds(exc)
            if wait is None:
                wait = min(delay, RETRY_MAX_DELAY)
                delay *= 2
            wait = min(wait, RETRY_MAX_DELAY)
            wait += random.uniform(0, wait * 0.25)
            if on_retry:
                on_retry(attempt, max_attempts, wait, exc)
            time.sleep(wait)
    raise RuntimeError("unreachable: retry loop exited without returning")


def _invoke_json(
    llm,
    messages: list,
    retries: int = 1,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    on_retry=None,
) -> dict:
    from langchain_core.messages import AIMessage, HumanMessage

    last_error: Exception | None = None
    for _ in range(retries + 1):
        response = _invoke_with_retry(llm, messages, max_attempts, on_retry=on_retry)
        content = message_text(response)
        try:
            return _extract_json(content)
        except (ValueError, json.JSONDecodeError) as err:
            last_error = err
            # Re-ask with a clean reminder. Echo the prior turn back only when it
            # has text: an empty assistant message has no content parts and Vertex
            # rejects the whole request ("must include at least one parts field").
            nudge: list = []
            prior = content.strip()
            if prior:
                nudge.append(AIMessage(content=prior))
            nudge.append(
                HumanMessage(
                    content=(
                        "Your previous response was not valid JSON. "
                        "Return ONLY the JSON object described, with no prose or code fences."
                    )
                )
            )
            messages = [*messages, *nudge]
    raise ValueError(f"LLM did not return valid JSON: {last_error}")


def generate_interlinks(
    doc,
    slug: str,
    inventory: list[InventoryItem],
    llm,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    on_retry=None,
) -> InterlinkAnalysis:
    """Call the LLM for one source and return its raw analysis."""
    from langchain_core.messages import HumanMessage, SystemMessage

    messages = [
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(content=build_user_prompt(slug, doc, inventory)),
    ]
    data = _invoke_json(llm, messages, max_attempts=max_attempts, on_retry=on_retry)

    links = data.get("links") or data.get("interlinks") or []
    if not isinstance(links, list):
        links = []

    primary_exam = data.get("primaryExam") or data.get("primary_exam")
    if isinstance(primary_exam, str):
        primary_exam = primary_exam.strip() or None
    else:
        primary_exam = None

    # The frontmatter examCodes are authoritative for quiz/exam linking; fall back to
    # the first one when the model did not identify a primary exam.
    codes = frontmatter_exam_codes(doc.frontmatter)
    if primary_exam is None and codes:
        primary_exam = codes[0]

    return InterlinkAnalysis(
        source_slug=slug,
        primary_exam=primary_exam,
        primary_topic=str(data.get("primaryTopic") or data.get("primary_topic") or "").strip(),
        audience=str(data.get("audience") or "").strip(),
        no_exam_found=bool(data.get("noExamFound") or data.get("no_exam_found") or primary_exam is None),
        links=links,
    )


# --- Guardrails -------------------------------------------------------------

def sanitize_anchor(text: str) -> str:
    """Normalise anchor text: drop em dashes / exclamation marks, collapse spaces."""
    text = text or ""
    for char in ("\u2014", "\u2013", "!"):
        text = text.replace(char, " ")
    return capitalize_anchor(re.sub(r"\s+", " ", text).strip())


def capitalize_anchor(text: str) -> str:
    """Sentence-case an anchor: uppercase its first letter, leave the rest unchanged.

    Anchors must read as sentence-case link text, but a leading exam code (``AI-103``)
    or number is left alone. Any leading punctuation (a quote or bracket) is skipped.
    """
    if not text:
        return text
    for index, char in enumerate(text):
        if char.isalpha():
            return f"{text[:index]}{char.upper()}{text[index + 1:]}"
        if char.isdigit():
            return text
    return text


def anchor_is_banned(text: str) -> bool:
    low = text.lower()
    return any(phrase in low for phrase in BANNED_ANCHOR_PHRASES)


def anchor_from_title(title: str) -> str:
    """Derive a <=7 word anchor from an SEO title (used for fallback links)."""
    base = (title or "").split("|")[0].split(":")[0].strip()
    words = base.split()[:MAX_ANCHOR_WORDS]
    return sanitize_anchor(" ".join(words))


def _public(link: dict) -> dict:
    return {key: link[key] for key in ("slug", "text", "position", "type", "reason")}


def _coerce_link(raw: dict, inventory: list[InventoryItem], source_slug: str, primary_exam: str | None, warnings: list[str]) -> dict | None:
    slug = str(raw.get("slug", "")).strip()
    type_ = str(raw.get("type", "")).strip()
    position = str(raw.get("position", "")).strip()
    text = sanitize_anchor(str(raw.get("text", "")))
    reason = str(raw.get("reason", "")).strip()

    try:
        score = int(raw.get("score", 0))
    except (TypeError, ValueError):
        score = 0

    if not slug:
        return None
    if position == "100%":
        warnings.append(f"moved {slug} from 100% to related (end-of-article links belong in Related Reading)")
        position = "related"
    if slug == source_slug:
        warnings.append(f"dropped self-link: {slug}")
        return None

    item = find(inventory, slug, type_)
    if item is None:
        warnings.append(f"dropped unknown inventory item: {slug} ({type_})")
        return None
    if position not in POSITIONS:
        warnings.append(f"dropped {slug}: invalid position '{position}'")
        return None
    if score < MIN_SCORE:
        warnings.append(f"dropped {slug}: score {score} below {MIN_SCORE}")
        return None
    if not text:
        warnings.append(f"dropped {slug}: empty anchor text")
        return None
    if anchor_is_banned(text):
        warnings.append(f"dropped {slug}: banned anchor phrase '{text}'")
        return None
    if len(text.split()) > MAX_ANCHOR_WORDS:
        truncated = " ".join(text.split()[:MAX_ANCHOR_WORDS])
        warnings.append(f"truncated anchor '{text}' -> '{truncated}'")
        text = truncated

    if type_ == "quiz":
        if not primary_exam or not item_matches_code(item, primary_exam):
            warnings.append(f"dropped quiz {slug}: not the primary exam ({primary_exam or 'none'})")
            return None
        if position not in QUIZ_POSITIONS:
            warnings.append(f"dropped quiz {slug}: position must be 20%, 50% or 75%")
            return None
    if position == "top" and type_ in {"exam", "quiz"}:
        warnings.append(f"dropped {slug}: top must not be an exam or quiz")
        return None

    return {"slug": slug, "text": text, "position": position, "type": type_, "reason": reason, "score": score}


def _fallback_item(
    inventory: list[InventoryItem],
    used: set[tuple[str, str]],
    *,
    types: tuple[str, ...],
    exclude_slug: str,
    code: str | None = None,
    slugs: tuple[str, ...] | None = None,
) -> InventoryItem | None:
    for item in inventory:
        if item.type not in types or item.key in used or item.slug == exclude_slug:
            continue
        if slugs is not None and item.slug not in slugs:
            continue
        if code and not item_matches_code(item, code):
            continue
        return item
    return None


def _fallback_top(inventory, used, primary_exam, source_slug) -> InventoryItem | None:
    if primary_exam:
        for types in (("study-plan",), ("roadmap",)):
            item = _fallback_item(
                inventory, used, types=types, exclude_slug=source_slug, code=primary_exam
            )
            if item:
                return item
    return _fallback_item(
        inventory,
        used,
        types=("roadmap",),
        exclude_slug=source_slug,
        slugs=("which-certification-to-start", "which-it-career-path", "which-cloud-path"),
    )


def _fallback_related(inventory, used, primary_exam, source_slug, need: int = 1) -> list[InventoryItem]:
    picks: list[InventoryItem] = []
    if not primary_exam:
        return picks
    for types in (("exam",), ("blog",), ("roadmap",), ("study-plan",)):
        item = _fallback_item(
            inventory, used, types=types, exclude_slug=source_slug, code=primary_exam
        )
        if item:
            picks.append(item)
            used.add(item.key)
            if len(picks) >= need:
                break
    return picks


def _resolve_related(related: list[dict], warnings: list[str]) -> list[dict]:
    """In the Related section, never keep both a quiz and its exam for one slug."""
    by_slug: dict[str, list[dict]] = {}
    for link in related:
        by_slug.setdefault(link["slug"], []).append(link)
    resolved: list[dict] = []
    for slug, group in by_slug.items():
        if len(group) > 1 and any(link["type"] == "quiz" for link in group):
            keep = [link for link in group if link["type"] != "quiz"]
            if not keep:
                keep = group
            else:
                warnings.append(f"dropped quiz {slug} from related: exam already present")
            resolved.extend(keep)
        else:
            resolved.extend(group)
    return resolved


def _find_item(
    inventory: list[InventoryItem], type_: str, code: str
) -> InventoryItem | None:
    """The inventory item of ``type_`` for an exam ``code`` (slug/title/description)."""
    for item in inventory:
        if item.type == type_ and item_matches_code(item, code):
            return item
    return None


def _link_from_item(
    item: InventoryItem, position: str, score: int, reason: str, text: str | None = None
) -> dict:
    return {
        "slug": item.slug,
        "text": text or anchor_from_title(item.seo_title),
        "position": position,
        "type": item.type,
        "reason": reason,
        "score": score,
    }


def _preferred_foundational(
    inventory: list[InventoryItem], primary_exam: str | None, source_slug: str
) -> InventoryItem | None:
    """A study plan, then roadmap, for the source exam (the preferred top link)."""
    if not primary_exam:
        return None
    for types in (("study-plan",), ("roadmap",)):
        item = _fallback_item(
            inventory, set(), types=types, exclude_slug=source_slug, code=primary_exam
        )
        if item is not None:
            return item
    return None


def _dedupe_targets(links: list[dict], warnings: list[str]) -> list[dict]:
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for link in links:
        key = (link["slug"], link["type"])
        if key in seen:
            warnings.append(f"dropped duplicate target {link['slug']} ({link['type']})")
            continue
        seen.add(key)
        out.append(link)
    return out


def _dedupe_anchors(links: list[dict], warnings: list[str]) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for link in links:
        key = link["text"].lower()
        if key in seen:
            warnings.append(f"dropped {link['slug']}: duplicate anchor text '{link['text']}'")
            continue
        seen.add(key)
        out.append(link)
    return out


def finalize_links(
    raw_links: list[dict],
    *,
    source_slug: str,
    primary_exam: str | None,
    inventory: list[InventoryItem],
    exam_codes: list[str] | None = None,
) -> tuple[list[dict], list[str]]:
    """Apply every hard rule and return (public_links, warnings).

    The source's frontmatter ``examCodes`` drive quiz and exam links: at least the first
    code's quiz is placed at an inline slot, one quiz per code up to three, and the exam
    page(s) go in the related section. The top link prefers a study plan / roadmap for the
    primary exam; related holds 3-9 items.
    """
    warnings: list[str] = []
    valid = [
        link
        for link in (
            _coerce_link(raw, inventory, source_slug, primary_exam, warnings) for raw in (raw_links or [])
        )
        if link is not None
    ]

    ordered = sorted(valid, key=lambda link: (POSITION_ORDER[link["position"]], -link["score"]))
    deduped = _dedupe_anchors(_dedupe_targets(ordered, warnings), warnings)

    codes = [str(code).strip() for code in (exam_codes or []) if str(code).strip()]
    effective_primary = primary_exam or (codes[0] if codes else None)

    quiz_items: dict[str, InventoryItem] = {}
    exam_items: dict[str, InventoryItem] = {}
    for code in codes:
        quiz = _find_item(inventory, "quiz", code)
        exam = _find_item(inventory, "exam", code)
        if quiz is not None:
            quiz_items.setdefault(code.upper(), quiz)
        if exam is not None:
            exam_items.setdefault(code.upper(), exam)

    # When the source lists examCodes, only those exams' quizzes may be linked.
    if codes:
        allowed_quizzes = {item.slug for item in quiz_items.values()}
        stray = [link for link in deduped if link["type"] == "quiz" and link["slug"] not in allowed_quizzes]
        for link in stray:
            warnings.append(f"dropped quiz {link['slug']}: not in the source examCodes")
        deduped = [link for link in deduped if link not in stray]

    related: list[dict] = []

    # --- top link (prefer a foundational study plan / roadmap) -----------------
    top_candidates = [link for link in deduped if link["position"] == "top"]
    top = max(top_candidates, key=lambda link: link["score"]) if top_candidates else None
    for extra in top_candidates:
        if extra is not top:
            warnings.append(f"dropped {extra['slug']}: top already taken by {top['slug']}")

    foundational = _preferred_foundational(inventory, effective_primary, source_slug)
    if foundational is not None and (top is None or top["type"] not in FOUNDATIONAL_TYPES):
        if top is not None:
            demoted = dict(top)
            demoted["position"] = "related"
            related.append(demoted)
            warnings.append(f"top: replaced {top['slug']} with foundational {foundational.slug}")
        top = _link_from_item(
            foundational,
            "top",
            TOP_MIN_SCORE,
            "Foundational roadmap/study plan for the source's primary exam.",
        )
    if top is None:
        item = _fallback_top(inventory, set(), effective_primary, source_slug)
        if item is not None:
            top = _link_from_item(
                item,
                "top",
                TOP_MIN_SCORE,
                "Relaxed fallback: no top candidate scored 80. Best foundational match for the source.",
            )
            warnings.append(f"relaxed top fallback -> {item.slug}")
        else:
            warnings.append("ERROR: no top link could be assigned")
    elif top["score"] < TOP_MIN_SCORE:
        warnings.append(f"relaxed top: '{top['slug']}' scored {top['score']} (below {TOP_MIN_SCORE})")

    used_keys: set[tuple[str, str]] = set()
    if top is not None:
        used_keys.add((top["slug"], top["type"]))

    # --- inline slots: required quizzes first, then the LLM's other links -------
    inline_candidates = [link for link in deduped if link["position"] in QUIZ_POSITIONS]
    llm_nonquiz = [link for link in inline_candidates if link["type"] != "quiz"]
    existing_quizzes = {link["slug"]: link for link in deduped if link["type"] == "quiz"}

    final_inline: dict[str, dict] = {}
    placed_quizzes: set[str] = set()
    for code, item in quiz_items.items():
        if len(final_inline) >= MAX_QUIZZES:
            break
        if item.slug in placed_quizzes:
            continue
        slot = next((pos for pos in QUIZ_SLOT_ORDER if pos not in final_inline), None)
        if slot is None:
            break
        existing = existing_quizzes.get(item.slug)
        link = dict(existing) if existing else _link_from_item(
            item,
            slot,
            90,
            f"Required quiz for exam code {code} (source examCodes).",
            text=f"{code} Practice Quiz",
        )
        link["position"] = slot
        final_inline[slot] = link
        placed_quizzes.add(item.slug)
        used_keys.add((item.slug, "quiz"))

    # Honour any quiz the LLM selected (e.g. when the source has no examCodes) that the
    # required-quiz loop did not already place.
    for link in inline_candidates:
        if link["type"] != "quiz" or link["slug"] in placed_quizzes:
            continue
        slot = next((pos for pos in QUIZ_SLOT_ORDER if pos not in final_inline), None)
        if slot is None:
            break
        placed = dict(link)
        placed["position"] = slot
        final_inline[slot] = placed
        placed_quizzes.add(link["slug"])
        used_keys.add((link["slug"], link["type"]))

    for position in QUIZ_POSITIONS:
        if position in final_inline:
            continue
        candidates = [
            link
            for link in llm_nonquiz
            if link["position"] == position and (link["slug"], link["type"]) not in used_keys
        ]
        if not candidates:
            continue
        candidate = max(candidates, key=lambda link: link["score"])
        placed = dict(candidate)
        placed["position"] = position
        final_inline[position] = placed
        used_keys.add((candidate["slug"], candidate["type"]))

    # Any remaining LLM inline (non-quiz) link is kept as a related item instead.
    for link in llm_nonquiz:
        if (link["slug"], link["type"]) in used_keys:
            continue
        moved = dict(link)
        moved["position"] = "related"
        related.append(moved)
        used_keys.add((link["slug"], link["type"]))
        if link["position"] in final_inline:
            warnings.append(
                f"moved {link['slug']} to related: inline position {link['position']} "
                f"taken by {final_inline[link['position']]['slug']}"
            )
        else:
            warnings.append(f"moved {link['slug']} to related")
    for link in inline_candidates:
        if link["type"] == "quiz" and link["slug"] not in placed_quizzes:
            warnings.append(f"dropped quiz {link['slug']}: no free inline slot")

    # --- related: LLM related items + one exam page per examCode ----------------
    related.extend(link for link in deduped if link["position"] == "related")
    for code, item in exam_items.items():
        if len(related) >= MAX_RELATED:
            break
        if (item.slug, "exam") in used_keys:
            continue
        related.append(
            _link_from_item(
                item,
                "related",
                80,
                f"Exam page for {code} (source examCodes).",
                text=f"{code} Exam Overview",
            )
        )
        used_keys.add((item.slug, "exam"))

    related = _resolve_related(related, warnings)
    if top is not None:
        related = [link for link in related if (link["slug"], link["type"]) != (top["slug"], top["type"])]
    inline_keys = {(link["slug"], link["type"]) for link in final_inline.values()}
    related = [link for link in related if (link["slug"], link["type"]) not in inline_keys]
    related = _dedupe_anchors(_dedupe_targets(related, warnings), warnings)
    related.sort(key=lambda link: -link["score"])
    if len(related) > MAX_RELATED:
        warnings.append(f"trimmed related links from {len(related)} to {MAX_RELATED}")
        related = related[:MAX_RELATED]
    if len(related) < MIN_RELATED:
        used = used_keys | {(link["slug"], link["type"]) for link in related}
        for item in _fallback_related(
            inventory, used, effective_primary, source_slug, need=MIN_RELATED - len(related)
        ):
            related.append(
                _link_from_item(
                    item,
                    "related",
                    MIN_SCORE,
                    "Relaxed fallback: same exam as the source.",
                )
            )
            warnings.append(f"relaxed related fallback -> {item.slug}")
        related.sort(key=lambda link: -link["score"])

    final: list[dict] = []
    if top is not None:
        final.append(_public(top))
    for position in QUIZ_POSITIONS:
        if position in final_inline:
            final.append(_public(final_inline[position]))
    final.extend(_public(link) for link in related)
    return _dedupe_anchors(final, warnings), warnings
