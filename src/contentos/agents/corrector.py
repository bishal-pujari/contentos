"""Agent 5: corrector — deterministic repairs plus targeted LLM rewrites.

Runs over content files and fixes:
1. FAQ heading variants (``## FAQ``, ``## Fast Answers``, ...) -> the required
   ``## Frequently Asked Questions (FAQ)``.
2. Dangling ``Other related exams:`` lines left behind by the link stripper.
3. SEO metadata: rewrites ``title`` / ``seoTitle`` / ``seoDescription`` when a field
   is more than 10% above its maximum or the primary keyword is missing, and places
   the primary keyword in the first 100 words (a minimal opening rewrite).
4. A missing FAQ section: four reader questions, generated with Google Search
   grounding when available so exam facts are verified against vendor docs.

The LLM only *proposes* values (JSON); this module validates and applies them, so a
bad model response can never produce an out-of-range title or a broken frontmatter.
Content is never mutated in place by the agent itself.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

import yaml

from contentos.agents.interlinker import DEFAULT_MAX_ATTEMPTS, _invoke_json
from contentos.agents.rater import is_exam_specific
from contentos.core.loaders import load_document
from contentos.core.protected import count_guarded, replace_guarded

DEFAULT_CORRECTION_PROMPT = os.path.join("prompts", "examos-blog-corrections.md")

FAQ_HEADING = "## Frequently Asked Questions (FAQ)"
FAQ_HEADING_RE = re.compile(
    r"^#{1,4}[ \t]+(?:FAQ|Frequently Asked Questions(?:\s*\(FAQ\))?|Fast Answers|Quick Answers)[ \t]*$",
    re.IGNORECASE,
)
TRUNCATED_LINE_RE = re.compile(r"^\s*Other\s+[Rr]elated\s+[Ee]xams:\s*$")
FIRST_WORDS_COUNT = 100
OVER_RATIO = 1.10

# Rewrites must not balloon or collapse relative to the original text. This is the
# guard against an LLM rambling or hallucinating: a proposal whose length ratio falls
# outside the band is rejected outright. Grammar fixes may expand a short bullet into a
# full sentence, so they get a wider band than de-slop or factual fixes.
PHRASE_MAX_GROWTH = 3.0
PHRASE_MIN_SHRINK = 0.30
SLOP_MAX_GROWTH = 1.8
SLOP_MIN_SHRINK = 0.50
FACT_MAX_GROWTH = 1.5
FACT_MIN_SHRINK = 0.50


def _within_length_band(find: str, replace: str, max_growth: float, min_shrink: float) -> bool:
    if not find:
        return False
    ratio = len(replace) / len(find)
    return min_shrink <= ratio <= max_growth

SEO_CONTRACT = """## OUTPUT (SEO TASK)
Return ONLY a JSON object. No markdown, no prose, no code fence:
{
  "primary_keyword": "the shortest natural phrase for the topic, usually the exam code (e.g. AI-103) or a 2-4 word topic; never an 'X vs Y' phrase",
  "title": "candidate title, 50-65 characters",
  "seoTitle": "candidate SEO title, 55-65 characters",
  "seoDescription": "candidate meta description, 140-160 characters",
  "opening_rewrite": "rewritten opening paragraph that contains the primary keyword, or an empty string when the first 100 words already contain it"
}"""

FAQ_CONTRACT = """## OUTPUT (FAQ TASK)
Return ONLY a JSON object. No markdown, no prose, no code fence:
{{
  "faq": [
    {{"question": "a real reader question", "answer": "1-3 specific sentences"}}
  ],
  "sources": ["https://official-source-url"]
}}
Return exactly {count} questions. Only assert what you can verify; if Google Search is
available, use it to check every exam-specific fact."""


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text).lower()).strip()


@dataclass(frozen=True)
class CorrectionSpec:
    name: str
    title_min: int
    title_max: int
    seo_title_min: int
    seo_title_max: int
    seo_description_min: int
    seo_description_max: int
    faq_questions: int
    system_prompt: str


def load_correction_spec(path: str = DEFAULT_CORRECTION_PROMPT) -> CorrectionSpec:
    """Load a correction prompt: YAML frontmatter config + system prompt body."""
    doc = load_document(path)
    if not doc.has_frontmatter:
        raise ValueError(f"Correction prompt needs YAML frontmatter: {path}")
    try:
        config = yaml.safe_load(doc.frontmatter) or {}
    except yaml.YAMLError as err:
        raise ValueError(f"Invalid YAML frontmatter in {path}: {err}") from err

    return CorrectionSpec(
        name=str(config.get("name") or os.path.splitext(os.path.basename(path))[0]),
        title_min=int(config.get("title_min", 50)),
        title_max=int(config.get("title_max", 65)),
        seo_title_min=int(config.get("seo_title_min", 55)),
        seo_title_max=int(config.get("seo_title_max", 70)),
        seo_description_min=int(config.get("seo_description_min", 140)),
        seo_description_max=int(config.get("seo_description_max", 160)),
        faq_questions=int(config.get("faq_questions", 4)),
        system_prompt=doc.body.strip(),
    )


# --- Frontmatter field access (targeted line edits, never a full re-dump) -----


def get_field(frontmatter: str, key: str) -> str | None:
    match = re.search(rf"^{re.escape(key)}:\s*(.*)$", frontmatter, re.MULTILINE)
    if not match:
        return None
    value = match.group(1).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        quote = value[0]
        value = value[1:-1]
        if quote == '"':
            value = value.replace("\\\\", "\x00").replace('\\"', '"').replace("\x00", "\\")
    return value


def _yaml_scalar(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def set_field(frontmatter: str, key: str, value: str) -> str:
    line = f"{key}: {_yaml_scalar(value)}"
    pattern = re.compile(rf"^{re.escape(key)}:[^\n]*$", re.MULTILINE)
    if pattern.search(frontmatter):
        return pattern.sub(lambda _match: line, frontmatter, count=1)
    if frontmatter and not frontmatter.endswith("\n"):
        return f"{frontmatter}\n{line}"
    return f"{frontmatter}{line}"


# --- Deterministic body fixes -------------------------------------------------


def normalize_faq_heading(body: str) -> tuple[str, bool]:
    """Rewrite the first FAQ-style heading to the required heading."""
    out: list[str] = []
    changed = False
    for line in body.splitlines(keepends=True):
        stripped = line.rstrip("\r\n")
        if not changed and stripped != FAQ_HEADING and FAQ_HEADING_RE.match(stripped):
            newline = "\n" if line.endswith("\n") else ""
            out.append(f"{FAQ_HEADING}{newline}")
            changed = True
        else:
            out.append(line)
    return "".join(out), changed


def has_faq_heading(body: str) -> bool:
    return any(FAQ_HEADING_RE.match(line.rstrip("\r\n")) for line in body.splitlines())


def remove_truncated_endings(body: str) -> tuple[str, int]:
    """Drop dangling ``Other related exams:`` lines and collapse blank runs."""
    lines = body.splitlines(keepends=True)
    kept = [line for line in lines if not TRUNCATED_LINE_RE.match(line.rstrip("\r\n"))]
    removed = len(lines) - len(kept)
    text = re.sub(r"\n{3,}", "\n\n", "".join(kept))
    return text, removed


# --- FAQ section analysis -----------------------------------------------------

QUESTION_LINE_RE = re.compile(r"^(?:###\s+\S|\*\*.+\?\*\*\s*$)")


def _faq_section_bounds(body: str) -> tuple[int, int] | None:
    """Line span of the FAQ section: the heading up to the next H2 (or EOF)."""
    lines = body.splitlines(keepends=True)
    start = None
    for index, line in enumerate(lines):
        if FAQ_HEADING_RE.match(line.rstrip("\r\n")):
            start = index
            break
    if start is None:
        return None
    end = len(lines)
    for index in range(start + 1, len(lines)):
        stripped = lines[index].rstrip("\r\n")
        # The FAQ section ends at the next H2 or a horizontal rule; anything after
        # that (closing CTA, related line) must be preserved.
        if re.match(r"^##\s", stripped) or stripped.strip() == "---":
            end = index
            break
    return start, end


def count_faq_questions(body: str) -> int:
    """Count ``### question`` / ``**question?**`` entries inside the FAQ section."""
    bounds = _faq_section_bounds(body)
    if bounds is None:
        return 0
    start, end = bounds
    section = body.splitlines()[start + 1 : end]
    return sum(1 for line in section if QUESTION_LINE_RE.match(line.strip()))


def replace_faq_section(body: str, replacement: str) -> str:
    bounds = _faq_section_bounds(body)
    if bounds is None:
        return body
    start, end = bounds
    lines = body.splitlines(keepends=True)
    return "".join(lines[:start]) + replacement + "".join(lines[end:])


# --- SEO analysis -------------------------------------------------------------


def _plain_text(body: str) -> str:
    text = re.sub(r"```.*?```", " ", body, flags=re.DOTALL)
    text = re.sub(r"`[^`]*`", " ", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"^#{1,6}\s*", " ", text, flags=re.MULTILINE)
    return re.sub(r"[*_>#]", " ", text)


def keyword_in_first_words(body: str, keyword: str, count: int = FIRST_WORDS_COUNT) -> bool:
    if not keyword:
        return True
    words = _normalize(_plain_text(body)).split()
    return _normalize(keyword) in " ".join(words[:count])


def _contains(text: str | None, keyword: str) -> bool:
    return bool(keyword) and _normalize(keyword) in _normalize(text or "")


def _over(value: str | None, maximum: int, ratio: float = OVER_RATIO) -> bool:
    return bool(value) and len(value) > maximum * ratio


def fields_needing_rewrite(
    spec: CorrectionSpec, frontmatter: str, keyword: str
) -> list[str]:
    """SEO fields to rewrite: more than 10% over the max, or missing the keyword."""
    title = get_field(frontmatter, "title")
    seo_title = get_field(frontmatter, "seoTitle")
    seo_desc = get_field(frontmatter, "seoDescription")
    needs: list[str] = []
    if _over(title, spec.title_max) or not _contains(title, keyword):
        needs.append("title")
    if _over(seo_title, spec.seo_title_max) or not _contains(seo_title, keyword):
        needs.append("seoTitle")
    if not seo_desc or _over(seo_desc, spec.seo_description_max) or not _contains(
        seo_desc, keyword
    ):
        needs.append("seoDescription")
    return needs


def _valid_meta(candidate, keyword: str, maximum: int, floor: int) -> bool:
    if not isinstance(candidate, str):
        return False
    candidate = candidate.strip()
    if not candidate or len(candidate) > maximum or len(candidate) < floor:
        return False
    return _contains(candidate, keyword)


# --- LLM passes ---------------------------------------------------------------


def build_seo_prompt(slug: str, doc, spec: CorrectionSpec) -> str:
    title = get_field(doc.frontmatter, "title") or ""
    seo_title = get_field(doc.frontmatter, "seoTitle") or ""
    seo_desc = get_field(doc.frontmatter, "seoDescription") or ""
    intro_words = " ".join(_plain_text(doc.body).split()[:FIRST_WORDS_COUNT])
    return (
        f"SOURCE SLUG: {slug}\n"
        f"FILENAME: {os.path.basename(doc.path)}\n\n"
        f"CURRENT METADATA:\n"
        f"title ({len(title)} chars): {title}\n"
        f"seoTitle ({len(seo_title)} chars): {seo_title}\n"
        f"seoDescription ({len(seo_desc)} chars): {seo_desc}\n\n"
        f"FIRST {FIRST_WORDS_COUNT} WORDS:\n{intro_words}\n\n"
        f"FULL FRONTMATTER:\n{doc.frontmatter or '(none)'}\n\n"
        f"Targets: title {spec.title_min}-{spec.title_max}, "
        f"seoTitle {spec.seo_title_min}-{spec.seo_title_max}, "
        f"seoDescription {spec.seo_description_min}-{spec.seo_description_max} chars."
    )


def build_faq_prompt(slug: str, doc, spec: CorrectionSpec) -> str:
    return (
        f"SOURCE SLUG: {slug}\n"
        f"FILENAME: {os.path.basename(doc.path)}\n\n"
        f"FRONTMATTER:\n{doc.frontmatter or '(none)'}\n\n"
        f"SOURCE BODY:\n{doc.body}\n\n"
        f"Write exactly {spec.faq_questions} FAQ entries for this post."
    )


def _invoke_correction(llm, system: str, user: str, max_attempts: int, on_retry) -> dict:
    from langchain_core.messages import HumanMessage, SystemMessage

    messages = [SystemMessage(content=system), HumanMessage(content=user)]
    data = _invoke_json(llm, messages, max_attempts=max_attempts, on_retry=on_retry)
    if not isinstance(data, dict):
        raise TypeError("correction response was not a JSON object")
    return data


def generate_seo(doc, slug: str, llm, spec: CorrectionSpec, max_attempts: int, on_retry) -> dict:
    system = f"{spec.system_prompt}\n\n{SEO_CONTRACT}"
    return _invoke_correction(llm, system, build_seo_prompt(slug, doc, spec), max_attempts, on_retry)


def generate_faq(
    doc, slug: str, llm, spec: CorrectionSpec, max_attempts: int, on_retry
) -> dict:
    system = f"{spec.system_prompt}\n\n{FAQ_CONTRACT.format(count=spec.faq_questions)}"
    return _invoke_correction(llm, system, build_faq_prompt(slug, doc, spec), max_attempts, on_retry)


FACT_CONTRACT = """## OUTPUT (FACT TASK)
First find every exam code, certification name, price, date, question count and passing
score in the body, then verify each against the vendor's CURRENT official page
(Microsoft Learn, AWS, ISC2, PMI, CompTIA). Exam codes change often: confirm the current
code for every certification named before deciding it is correct. Use Google Search.
Return ONLY a JSON object. No markdown, no prose, no code fence:
{
  "issues": [
    {"find": "the exact wrong text as it appears",
     "replace": "the corrected text",
     "reason": "what was wrong",
     "source": "https://official-vendor-url"}
  ]
}
Only report concrete, verifiable errors. Return an empty list when every fact is correct.
The `find` string must be copied exactly as it appears (it may occur more than once)."""

GRAMMAR_CONTRACT = """## OUTPUT (GRAMMAR TASK)
Fix only broken grammar: sentence fragments, run-on sentences, dangling modifiers,
missing verbs, unclosed quotes or parentheses, and subject-verb disagreement. A phrase
that lacks both a subject and a verb is a fragment, even in a short bullet description;
rewrite it as a complete sentence while keeping its meaning and any exam code exact
(for example "Most recognized cloud cert." becomes "It is the most recognized cloud
certification."). Do not change facts or tone you merely dislike, and do not merge
separate bullet points into one paragraph.
Return ONLY a JSON object. No markdown, no prose, no code fence:
{
  "issues": [
    {"find": "the exact broken sentence or phrase",
     "replace": "the minimal corrected version",
     "reason": "fragment"}
  ]
}
Return an empty list when the grammar is fine. The `find` string must be copied exactly
and should be a whole sentence or phrase (at least 12 characters)."""

DESLOP_CONTRACT = """## OUTPUT (DE-SLOP TASK)
Remove AI-slop tells while preserving the meaning, the facts, the tone and any exam code
exactly. Rewrite the whole sentence that contains a tell so it reads naturally.
Tells to remove:
- empty intensifiers and hedges (very, really, truly, actually, genuinely, basically,
  simply, literally) - including inside headings ("How to Actually Prepare" -> "How to Prepare");
- filler phrases ("A quick note before diving in.", "Let's dive in", "The key insight is");
- the "It's not just X, it's Y" construction (state it plainly);
- scare quotes around an ordinary word: remove the quotation marks. Keep quotes only for
  direct source quotations, defined terms and quoted exam questions;
- em dashes and exclamation marks.
Return ONLY a JSON object. No markdown, no prose, no code fence:
{
  "issues": [
    {"find": "the exact sentence or heading as it appears",
     "replace": "the rewritten version with the slop removed",
     "reason": "empty intensifier"}
  ]
}
Return an empty list when there is no slop. The `find` string must be copied exactly;
the same string may occur more than once and every occurrence is replaced."""

EXAMOS_CONTRACT = """## OUTPUT (EXAMOS TASK)
ExamOS only has these features: Daily Sprint, Mock Exam, Readiness Score, and the
Rookie / Challenger / Legend practice modes. A booking recommendation is triggered by
the Readiness Score. There is no other feature, mode, dashboard, badge, plan or metric.
Find every invented feature or wrong booking trigger and rewrite the whole sentence to
remove it or replace it with a real feature, keeping the meaning and exam codes exact.
Return ONLY a JSON object. No markdown, no prose, no code fence:
{
  "issues": [
    {"find": "the exact sentence as it appears",
     "replace": "the rewritten sentence with the invented feature fixed",
     "reason": "invented feature"}
  ]
}
Return an empty list when every ExamOS reference is correct. The `find` string must be
copied exactly; the same string may occur more than once and every occurrence is replaced."""


def build_fact_prompt(slug: str, doc, body: str, spec: CorrectionSpec) -> str:
    return (
        f"SOURCE SLUG: {slug}\n"
        f"FILENAME: {os.path.basename(doc.path)}\n\n"
        f"SOURCE BODY:\n{body}\n\n"
        "Check every exam-specific fact against official vendor documentation."
    )


def generate_facts(
    body: str, slug: str, doc, llm, spec: CorrectionSpec, max_attempts: int, on_retry
) -> dict:
    system = f"{spec.system_prompt}\n\n{FACT_CONTRACT}"
    return _invoke_correction(
        llm, system, build_fact_prompt(slug, doc, body, spec), max_attempts, on_retry
    )


def build_grammar_prompt(slug: str, doc, body: str) -> str:
    return (
        f"SOURCE SLUG: {slug}\n"
        f"FILENAME: {os.path.basename(doc.path)}\n\n"
        f"SOURCE BODY:\n{body}"
    )


def generate_grammar(doc, slug: str, body: str, llm, spec: CorrectionSpec, max_attempts: int, on_retry) -> dict:
    system = f"{spec.system_prompt}\n\n{GRAMMAR_CONTRACT}"
    return _invoke_correction(
        llm, system, build_grammar_prompt(slug, doc, body), max_attempts, on_retry
    )


def generate_deslop(doc, slug: str, body: str, llm, spec: CorrectionSpec, max_attempts: int, on_retry) -> dict:
    system = f"{spec.system_prompt}\n\n{DESLOP_CONTRACT}"
    return _invoke_correction(
        llm, system, build_grammar_prompt(slug, doc, body), max_attempts, on_retry
    )


def _mentions_examos(body: str) -> bool:
    return "examos" in body.lower()


def generate_examos_fixes(doc, slug: str, body: str, llm, spec: CorrectionSpec, max_attempts: int, on_retry) -> dict:
    system = f"{spec.system_prompt}\n\n{EXAMOS_CONTRACT}"
    return _invoke_correction(
        llm, system, build_grammar_prompt(slug, doc, body), max_attempts, on_retry
    )


def _looks_like_fact(find: str) -> bool:
    """A code/date/price carries a digit or an uppercase run (so 'PMP' passes, 'the' does not)."""
    if len(find) < 3:
        return False
    return any(ch.isdigit() for ch in find) or any(ch.isupper() for ch in find)


# A markdown link or image. The corrector must never introduce one (the stripper and the
# writers own links); a replacement that adds a link to previously plain text is rejected.
_MD_LINK_RE = re.compile(r"!?\[[^\]]*\]\(")


def _adds_link(find: str, replace: str) -> bool:
    return bool(_MD_LINK_RE.search(replace)) and not _MD_LINK_RE.search(find)


def _apply_replacements(
    text: str, issues
) -> tuple[str, list[str], list[str], list[str]]:
    """Apply sourced find/replace fixes. Returns (text, applied, sources, warnings)."""
    applied: list[str] = []
    sources: list[str] = []
    warnings: list[str] = []
    for issue in issues if isinstance(issues, list) else []:
        if not isinstance(issue, dict):
            continue
        find = str(issue.get("find") or "")
        replace = str(issue.get("replace") or "")
        source = str(issue.get("source") or "").strip()
        if not find or not replace or find == replace:
            continue
        if not _looks_like_fact(find):
            warnings.append(f"fact fix skipped (ambiguous/short): {find!r}")
            continue
        if _adds_link(find, replace):
            warnings.append(f"fact fix skipped (would add a link): {find[:50]}")
            continue
        if not _within_length_band(find, replace, FACT_MAX_GROWTH, FACT_MIN_SHRINK):
            warnings.append(
                f"fact fix skipped (length changed too much): {find[:50]} -> {replace[:50]}"
            )
            continue
        occurrences = count_guarded(text, find)
        if occurrences == 0:
            continue
        if not source.startswith("http"):
            warnings.append(f"fact fix skipped (no source): {find[:60]}")
            continue
        text = replace_guarded(text, find, replace)
        suffix = f" (x{occurrences})" if occurrences > 1 else ""
        applied.append(f"{find} -> {replace}{suffix}")
        sources.append(source)
    return text, applied, sources, warnings


def _apply_phrase_replacements(
    text: str,
    issues,
    max_growth: float = PHRASE_MAX_GROWTH,
    min_shrink: float = PHRASE_MIN_SHRINK,
) -> tuple[str, list[str], list[str]]:
    """Apply minimal grammar/slop fixes. Returns (text, applied, warnings).

    A phrase must be at least 12 characters, every occurrence is replaced, and the
    replacement must stay within the length band (so a rambling or hallucinated rewrite
    is rejected).
    """
    applied: list[str] = []
    warnings: list[str] = []
    for issue in issues if isinstance(issues, list) else []:
        if not isinstance(issue, dict):
            continue
        find = str(issue.get("find") or "")
        replace = str(issue.get("replace") or "")
        if not find or not replace or find == replace:
            continue
        if len(find) < 12:
            warnings.append(f"phrase fix skipped (too short): {find[:60]}")
            continue
        if _adds_link(find, replace):
            warnings.append(f"phrase fix skipped (would add a link): {find[:50]}")
            continue
        if not _within_length_band(find, replace, max_growth, min_shrink):
            warnings.append(
                f"phrase fix skipped (length changed too much): {find[:50]} -> {replace[:50]}"
            )
            continue
        occurrences = count_guarded(text, find)
        if occurrences == 0:
            continue
        text = replace_guarded(text, find, replace)
        suffix = f" (x{occurrences})" if occurrences > 1 else ""
        applied.append(f"{find} -> {replace}{suffix}")
    return text, applied, warnings


# --- Applying the changes -----------------------------------------------------


@dataclass
class CorrectionResult:
    slug: str
    filename: str
    new_text: str
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    searched: bool = False
    review_grade: str = ""
    review_average: float = 0.0
    review_flagged: bool = False
    review_result: object | None = None


def _sanitize_faq(entries, expected: int) -> list[dict]:
    cleaned: list[dict] = []
    seen: set[str] = set()
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        question = str(entry.get("question") or "").strip().lstrip("#").strip()
        answer = str(entry.get("answer") or "").strip()
        if not question or not answer:
            continue
        key = _normalize(question)
        if key in seen:
            continue
        seen.add(key)
        cleaned.append({"question": question, "answer": answer})
        if len(cleaned) >= expected:
            break
    return cleaned


def _render_faq(entries: list[dict]) -> str:
    parts = [FAQ_HEADING, ""]
    for entry in entries:
        parts.append(f"### {entry['question']}")
        parts.append("")
        parts.append(entry["answer"])
        parts.append("")
    return "\n".join(parts).rstrip("\n") + "\n"


def generate_correction(
    doc,
    slug: str,
    spec: CorrectionSpec,
    llm,
    search_llm=None,
    verify_faq: bool = True,
    fix_facts: bool = True,
    fix_grammar: bool = True,
    fix_slop: bool = True,
    fix_examos: bool = True,
    seo: bool = False,
    faq_applies: bool = True,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    on_retry=None,
    search_budget=None,
) -> CorrectionResult:
    """Correct one document. The LLM proposes values; code validates and applies.

    SEO metadata is off by default: ``fix-frontmatter`` owns the title/seoTitle/
    seoDescription rewrite, so the corrector leaves those fields untouched unless
    ``seo`` is explicitly enabled.
    """
    result = CorrectionResult(slug=slug, filename=os.path.basename(doc.path), new_text="")

    frontmatter = doc.frontmatter
    body, heading_changed = normalize_faq_heading(doc.body)
    if heading_changed:
        result.notes.append("normalized FAQ heading")

    body, removed = remove_truncated_endings(body)
    if removed:
        result.notes.append(f"removed {removed} dangling 'Other related exams' line(s)")

    # SEO pass (one plain call: it also identifies the primary keyword). Off by
    # default so `fix-frontmatter` is the single SEO rewriter. Failures leave the
    # deterministic fixes intact and are recorded as warnings.
    if seo and doc.has_frontmatter:
        try:
            seo = generate_seo(doc, slug, llm, spec, max_attempts, on_retry)
        except Exception as err:  # noqa: BLE001 - surface, keep deterministic fixes
            seo = {}
            result.warnings.append(f"SEO rewrite failed: {type(err).__name__}: {err}")

        keyword = str(seo.get("primary_keyword") or "").strip()
        needs = fields_needing_rewrite(spec, frontmatter, keyword) if keyword else []

        candidates = {
            "title": (seo.get("title"), spec.title_max, spec.title_min, "title"),
            "seoTitle": (
                seo.get("seoTitle"),
                spec.seo_title_max,
                spec.seo_title_min,
                "seoTitle",
            ),
            "seoDescription": (
                seo.get("seoDescription"),
                spec.seo_description_max,
                spec.seo_description_min,
                "seoDescription",
            ),
        }
        for key in needs:
            candidate, maximum, floor, label = candidates[key]
            if _valid_meta(candidate, keyword, maximum, floor):
                before = get_field(frontmatter, key)
                frontmatter = set_field(frontmatter, key, candidate.strip())
                result.notes.append(
                    f"{label}: {len(before or '')} -> {len(candidate.strip())} chars"
                )
            else:
                result.warnings.append(f"{label} rewrite rejected (missing/out of range)")

        opening = str(seo.get("opening_rewrite") or "").strip()
        if keyword and not keyword_in_first_words(body, keyword):
            if opening and _contains(opening, keyword):
                body = _replace_opening(body, opening)
                result.notes.append(
                    "rewrote opening to place primary keyword in first 100 words"
                )
            else:
                result.warnings.append(
                    "primary keyword missing from first 100 words (no safe rewrite)"
                )

    # De-slop pass (plain LLM): remove empty intensifiers, filler and scare quotes.
    if fix_slop:
        try:
            slop = generate_deslop(doc, slug, body, llm, spec, max_attempts, on_retry)
        except Exception as err:  # noqa: BLE001 - keep the rest of the corrections
            slop = {}
            result.warnings.append(f"de-slop check failed: {type(err).__name__}: {err}")
        body, slop_notes, slop_warnings = _apply_phrase_replacements(
            body, slop.get("issues"), SLOP_MAX_GROWTH, SLOP_MIN_SHRINK
        )
        result.notes.extend(f"de-slop: {note}" for note in slop_notes)
        result.warnings.extend(slop_warnings)

    # Grammar pass (plain LLM): minimal, exact-match sentence fixes.
    if fix_grammar:
        try:
            grammar = generate_grammar(
                doc, slug, body, llm, spec, max_attempts, on_retry
            )
        except Exception as err:  # noqa: BLE001 - keep the rest of the corrections
            grammar = {}
            result.warnings.append(f"grammar check failed: {type(err).__name__}: {err}")
        body, grammar_notes, grammar_warnings = _apply_phrase_replacements(
            body, grammar.get("issues")
        )
        result.notes.extend(f"grammar: {note}" for note in grammar_notes)
        result.warnings.extend(grammar_warnings)

    # ExamOS pass (plain LLM): only when the post mentions ExamOS. Fixes invented
    # features / wrong booking triggers.
    if fix_examos and _mentions_examos(body):
        try:
            examos = generate_examos_fixes(doc, slug, body, llm, spec, max_attempts, on_retry)
        except Exception as err:  # noqa: BLE001 - keep the rest of the corrections
            examos = {}
            result.warnings.append(f"ExamOS check failed: {type(err).__name__}: {err}")
        body, examos_notes, examos_warnings = _apply_phrase_replacements(
            body, examos.get("issues")
        )
        result.notes.extend(f"ExamOS: {note}" for note in examos_notes)
        result.warnings.extend(examos_warnings)

    # FAQ pass: when the section is missing, or present with fewer than the required
    # questions, regenerate it so a short section can never be padded into duplicates.
    # Skipped entirely for content types whose profile sets faq.applies=false (roadmaps,
    # exam FAQ documents, learn), so no FAQ section is ever added.
    expected_faq = spec.faq_questions
    existing_faq = count_faq_questions(body)
    if faq_applies and (not has_faq_heading(body) or existing_faq < expected_faq):
        if verify_faq and search_llm is not None and _reserve(search_budget):
            faq_called = False
            try:
                faq_data = generate_faq(doc, slug, search_llm, spec, max_attempts, on_retry)
                faq_called = True
                result.searched = True
            except Exception as err:  # noqa: BLE001 - keep the rest of the corrections
                faq_data = {}
                result.warnings.append(f"FAQ generation failed: {type(err).__name__}: {err}")
            entries = _sanitize_faq(faq_data.get("faq"), expected_faq)
            if entries:
                if has_faq_heading(body):
                    body = replace_faq_section(body, _render_faq(entries) + "\n")
                    result.notes.append(
                        f"regenerated FAQ section ({existing_faq} -> {len(entries)} questions)"
                    )
                else:
                    body = body.rstrip("\n") + "\n\n" + _render_faq(entries)
                    result.notes.append(f"added FAQ section ({len(entries)} questions)")
                sources = faq_data.get("sources")
                if isinstance(sources, list):
                    result.sources.extend(
                        str(source).strip() for source in sources if str(source).strip()
                    )
            elif faq_called:
                result.warnings.append("FAQ generation returned no usable questions")
        else:
            result.warnings.append("FAQ missing/short and search verification unavailable")

    # Factual pass: run last, over the final body (including any regenerated FAQ), so
    # grounded corrections cover the FAQ text too. Applied only when the exact text is
    # unique and an official source is given.
    if fix_facts and search_llm is not None and is_exam_specific(slug, doc):
        if _reserve(search_budget):
            try:
                facts = generate_facts(
                    body, slug, doc, search_llm, spec, max_attempts, on_retry
                )
                result.searched = True
            except Exception as err:  # noqa: BLE001 - keep the rest of the corrections
                facts = {}
                result.warnings.append(f"fact check failed: {type(err).__name__}: {err}")
            body, fact_notes, fact_sources, fact_warnings = _apply_replacements(
                body, facts.get("issues")
            )
            result.notes.extend(f"fixed fact: {note}" for note in fact_notes)
            result.sources.extend(fact_sources)
            result.warnings.extend(fact_warnings)
        else:
            result.warnings.append("fact check skipped (web-search budget exhausted)")

    result.new_text = _render(frontmatter, body)
    return result


def _reserve(search_budget) -> bool:
    return search_budget is None or search_budget.try_acquire()


def _replace_opening(body: str, opening: str) -> str:
    """Replace the first prose paragraph after the H1 with ``opening``."""
    lines = body.splitlines(keepends=True)
    start = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if start is None:
            if stripped.startswith("#") or not stripped:
                continue
            start = index
            break
    if start is None:
        return body
    end = start
    while end < len(lines) and lines[end].strip():
        end += 1
    return "".join(lines[:start]) + opening.rstrip("\n") + "\n" + "".join(lines[end:])


def _render(frontmatter: str, body: str) -> str:
    if frontmatter:
        return f"---\n{frontmatter}\n---\n{body}"
    return body
