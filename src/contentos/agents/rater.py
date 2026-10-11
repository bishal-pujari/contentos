"""Generic content rating agent.

The rubric lives in an external prompt file (Markdown with YAML frontmatter) so
the same agent can grade different projects with different criteria:

```
---
name: ExamOS Blog Rating
criteria:
  - {key: factual_correctness, label: Factual Correctness, weight: 3}
  ...
grade_bands:
  - {min: 4.7, label: A+}
  ...
---
<system prompt body>
```

The LLM judges and scores every criterion (no deterministic pre-scoring); it
returns JSON per document. This module clamps the scores, computes the weighted
total and letter grade from the frontmatter config, and renders the report block
so the table, total and grade can never disagree.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

import yaml

from contentos.agents.interlinker import DEFAULT_MAX_ATTEMPTS, _invoke_json
from contentos.core.loaders import load_document

DEFAULT_PROMPT_PATH = os.path.join("prompts", "examos-blog-rating.md")

# Deterministic signals that a post is about a specific certification exam and
# therefore needs its exam details verified against official vendor docs.
KNOWN_EXAM_NAMES = (
    "cissp",
    "cism",
    "ccsp",
    "ccsk",
    "cisa",
    "crisc",
    "pmp",
    "capm",
    "pgmp",
    "comptia",
    "security plus",
    "network plus",
    "cysa",
    "pentest",
    "casp",
    "ccna",
    "ccnp",
    "ccie",
    "itil",
    "prince2",
    "six sigma",
    "scrum",
    "aws certified",
    "azure certified",
    "google cloud certified",
    "google cloud professional",
)
VENDOR_WORDS = {"aws", "azure", "microsoft", "gcp", "cloud", "isc2", "isaca", "pmi", "comptia"}
VENDOR_PHRASES = ("google cloud",)
EXAM_INTENT_WORDS = {
    "hard",
    "difficulty",
    "worth",
    "mandatory",
    "vs",
    "versus",
    "comparison",
    "compare",
    "practice",
    "blueprint",
    "domain",
    "domains",
    "objective",
    "objectives",
    "readiness",
    "exam",
    "exams",
    "certification",
    "certifications",
    "certified",
    "passing",
    "questions",
    "study",
    "guide",
    "plan",
    "test",
    "tests",
    "testing",
}

# Strict exam code (requires the hyphen) so a stray year like "for 2026" cannot
# masquerade as a code.
EXAM_CODE_RE = re.compile(r"\b[A-Za-z]{2,5}-\d{2,4}\b")

EXAM_SEARCH_ADDENDUM = (
    "This post is about a specific certification exam. When Google Search is available, "
    "verify every exam-specific detail (domains and weightings, number of questions, time "
    "limit, passing score, price, policies and dates) against the vendor's official "
    "documentation, such as Microsoft Learn, AWS, ISC2, PMI or CompTIA. Mark anything you "
    "still cannot confirm as [UNVERIFIABLE] and do not penalise it."
)


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text).lower())


def is_exam_specific(slug: str, doc) -> bool:
    """True when the post targets a specific exam/certification.

    Deterministic, based on the slug and frontmatter (title/seoTitle/description/
    categories) only — never the body, so a passing exam-code mention in prose does
    not count. True when an exam code or known exam name appears in the slug or
    title, or when a vendor token is combined with exam-intent wording.
    """
    frontmatter = doc.frontmatter or ""
    slug_norm = _normalize(slug)
    title_norm = _normalize(frontmatter)
    slug_tokens = set(slug_norm.split())
    title_tokens = set(title_norm.split())
    anchor_tokens = slug_tokens | title_tokens

    if EXAM_CODE_RE.search(slug) or EXAM_CODE_RE.search(frontmatter):
        return True
    if any(
        _normalize(name) in slug_norm or _normalize(name) in title_norm
        for name in KNOWN_EXAM_NAMES
    ):
        return True

    has_vendor = bool(anchor_tokens & VENDOR_WORDS) or any(
        phrase in slug_norm or phrase in title_norm for phrase in VENDOR_PHRASES
    )
    return has_vendor and bool(anchor_tokens & EXAM_INTENT_WORDS)


@dataclass(frozen=True)
class Criterion:
    key: str
    label: str
    weight: float


@dataclass(frozen=True)
class GradeBand:
    minimum: float
    label: str


@dataclass(frozen=True)
class Rubric:
    name: str
    criteria: tuple[Criterion, ...]
    grade_bands: tuple[GradeBand, ...]
    system_prompt: str

    @property
    def total_weight(self) -> float:
        return sum(criterion.weight for criterion in self.criteria)


def _normalize_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(key).strip().lower()).strip("_")


def _output_contract(criteria: tuple[Criterion, ...]) -> str:
    """Auto-generate the JSON output spec so prompts never hard-code keys."""
    lines = [
        "## OUTPUT",
        "Return ONLY a JSON object. No markdown, no prose, no code fence:",
        "{",
        '  "filename": "the source filename",',
        '  "scores": {',
    ]
    for index, criterion in enumerate(criteria):
        comma = "," if index < len(criteria) - 1 else ""
        lines.append(f'    "{criterion.key}": 1-5{comma}')
    lines.extend(
        [
            "  },",
            '  "strengths": ["1-3 concrete strengths"],',
            '  "weaknesses": ["1-3 concrete weaknesses"],',
            '  "factual_issues": ["each error, or each [UNVERIFIABLE] claim; empty array if none"],',
            '  "summary": "1-2 sentence verdict"',
            "}",
            "Score every criterion 1 to 5. Weighted total and letter grade are computed separately.",
        ]
    )
    return "\n".join(lines)


def load_rubric(path: str = DEFAULT_PROMPT_PATH) -> Rubric:
    """Load a prompt file: YAML frontmatter config + system prompt body."""
    doc = load_document(path)
    if not doc.has_frontmatter:
        raise ValueError(f"Prompt file needs YAML frontmatter: {path}")
    try:
        config = yaml.safe_load(doc.frontmatter) or {}
    except yaml.YAMLError as err:
        raise ValueError(f"Invalid YAML frontmatter in {path}: {err}") from err

    raw_criteria = config.get("criteria") or []
    if not raw_criteria:
        raise ValueError(f"Prompt file defines no criteria: {path}")
    criteria = tuple(
        Criterion(
            key=_normalize_key(entry["key"]),
            label=str(entry.get("label") or entry["key"]),
            weight=float(entry.get("weight", 1)),
        )
        for entry in raw_criteria
    )

    raw_bands = config.get("grade_bands") or []
    if not raw_bands:
        raise ValueError(f"Prompt file defines no grade_bands: {path}")
    grade_bands = tuple(
        GradeBand(minimum=float(band["min"]), label=str(band["label"])) for band in raw_bands
    )

    name = str(config.get("name") or os.path.splitext(os.path.basename(path))[0])
    body = doc.body.strip()
    system_prompt = f"{body}\n\n{_output_contract(criteria)}" if body else _output_contract(criteria)
    return Rubric(name=name, criteria=criteria, grade_bands=grade_bands, system_prompt=system_prompt)


def build_rating_prompt(slug: str, doc, exam_specific: bool = False) -> str:
    prompt = (
        f"SOURCE SLUG: {slug}\n"
        f"FILENAME: {os.path.basename(doc.path)}\n\n"
        f"FRONTMATTER:\n{doc.frontmatter or '(none)'}\n\n"
        f"SOURCE BODY:\n{doc.body}\n\n"
        "Return only the JSON object described in the system instructions."
    )
    if exam_specific:
        prompt += f"\n\n{EXAM_SEARCH_ADDENDUM}"
    return prompt


def clamp_score(value) -> float:
    """Coerce an LLM score to the 1-5 range (invalid input becomes 1)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 1.0
    return min(5.0, max(1.0, number))


def letter_grade(average: float, grade_bands: tuple[GradeBand, ...]) -> str:
    """Map a 0-5 weighted average to the rubric's letter bands (highest first)."""
    rounded = round(average, 2)
    for band in sorted(grade_bands, key=lambda item: item.minimum, reverse=True):
        if rounded >= band.minimum:
            return band.label
    return grade_bands[-1].label if grade_bands else "F"


@dataclass
class RatingResult:
    slug: str
    filename: str
    criteria: tuple[Criterion, ...]
    grade_bands: tuple[GradeBand, ...]
    scores: dict[str, float] = field(default_factory=dict)
    strengths: list[str] = field(default_factory=list)
    weaknesses: list[str] = field(default_factory=list)
    factual_issues: list[str] = field(default_factory=list)
    summary: str = ""
    warnings: list[str] = field(default_factory=list)
    searched: bool = False

    @property
    def total_weight(self) -> float:
        return sum(criterion.weight for criterion in self.criteria)

    @property
    def total(self) -> float:
        return sum(self.scores.get(c.key, 1.0) * c.weight for c in self.criteria)

    @property
    def average(self) -> float:
        weight = self.total_weight
        return self.total / weight if weight else 0.0

    @property
    def grade(self) -> str:
        return letter_grade(self.average, self.grade_bands)


def _as_list(value) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def parse_rating(data: dict, slug: str, filename: str, rubric: Rubric) -> RatingResult:
    """Validate raw LLM output, clamp scores and compute the graded result."""
    raw_scores = data.get("scores") or {}
    if not isinstance(raw_scores, dict):
        raw_scores = {}
    normalized = {_normalize_key(key): value for key, value in raw_scores.items()}

    result = RatingResult(
        slug=slug, filename=filename, criteria=rubric.criteria, grade_bands=rubric.grade_bands
    )
    for criterion in rubric.criteria:
        if criterion.key not in normalized:
            result.warnings.append(f"missing score for {criterion.label}; defaulted to 1")
        result.scores[criterion.key] = clamp_score(normalized.get(criterion.key, 1.0))

    result.strengths = _as_list(data.get("strengths"))
    result.weaknesses = _as_list(data.get("weaknesses"))
    result.factual_issues = _as_list(data.get("factual_issues") or data.get("factualIssues"))
    result.summary = str(data.get("summary") or "").strip()
    return result


def _needs_verification(result: RatingResult) -> bool:
    """True when the first pass flagged a claim it could not verify."""
    return any("[unverifiable]" in issue.lower() for issue in result.factual_issues)


def _used_fallback(llm) -> bool:
    """True when ``llm``'s most recent call on this thread used the fallback provider."""
    return bool(getattr(llm, "used_fallback", False))


def _invoke_rating(
    llm, doc, slug: str, rubric: Rubric, max_attempts: int, on_retry, exam_specific: bool = False
) -> RatingResult:
    from langchain_core.messages import HumanMessage, SystemMessage

    messages = [
        SystemMessage(content=rubric.system_prompt),
        HumanMessage(content=build_rating_prompt(slug, doc, exam_specific)),
    ]
    data = _invoke_json(llm, messages, max_attempts=max_attempts, on_retry=on_retry)
    if not isinstance(data, dict):
        raise TypeError("rating response was not a JSON object")
    return parse_rating(data, slug, os.path.basename(doc.path), rubric)


def generate_rating(
    doc,
    slug: str,
    llm,
    rubric: Rubric,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    on_retry=None,
    search_llm=None,
    verify_facts: bool = True,
    force_ground: bool = False,
    search_budget=None,
) -> RatingResult:
    """Rate one document, verifying exam-specific and unverifiable facts.

    Exam-specific posts (study guides, difficulty, worth-it, comparisons, ...) are
    rated on the first pass with Google Search grounding so their exam details are
    checked against official vendor docs. Every other post is rated without search
    first; it only gets a search pass when it flags a ``[UNVERIFIABLE]`` claim.

    ``force_ground`` makes the first pass grounded for every document regardless of
    the exam-specific heuristic (used by content types whose profile sets
    ``ground: always``, e.g. study plans and exam FAQ).

    ``search_budget`` (a ``SearchBudget``) caps how many grounded calls a run may
    make. When it is exhausted the post is returned unsearched with a warning.
    """
    exam = is_exam_specific(slug, doc)
    search_available = verify_facts and search_llm is not None

    def _reserve_search() -> bool:
        if not search_available:
            return False
        return search_budget is None or search_budget.try_acquire()

    def _budget_exhausted() -> bool:
        return search_available and search_budget is not None and search_budget.remaining == 0

    if exam or force_ground:
        if _reserve_search():
            result = _invoke_rating(
                search_llm, doc, slug, rubric, max_attempts, on_retry, exam_specific=True
            )
            result.searched = not _used_fallback(search_llm)
            if not result.searched:
                result.warnings.append(
                    "Google Search grounding was unavailable (the search call fell back to the "
                    "secondary provider); exam details were not verified"
                )
            return result
        result = _invoke_rating(llm, doc, slug, rubric, max_attempts, on_retry)
        if _budget_exhausted():
            result.warnings.append("web-search budget exhausted; exam details not verified")
        return result


    result = _invoke_rating(llm, doc, slug, rubric, max_attempts, on_retry)
    if not (search_available and _needs_verification(result)):
        return result
    if not _reserve_search():
        result.warnings.append("web-search budget exhausted; unverifiable claim left unresolved")
        return result
    try:
        verified = _invoke_rating(
            search_llm, doc, slug, rubric, max_attempts, on_retry, exam_specific=exam
        )
    except Exception as err:  # noqa: BLE001 - fall back to the first pass on any search failure
        result.warnings.append(f"web-search verification failed: {type(err).__name__}: {err}")
        return result
    verified.searched = not _used_fallback(search_llm)
    if not verified.searched:
        verified.warnings.append(
            "Google Search grounding was unavailable (the search call fell back to the "
            "secondary provider); the unverifiable claim was not checked"
        )
    verified.warnings = [*result.warnings, *verified.warnings]
    return verified


def _num(value: float) -> str:
    """Format a score/weight: integers stay integers, else one decimal."""
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.1f}"


def _numbered(items: list[str]) -> list[str]:
    if not items:
        return ["1. -"]
    return [f"{index}. {item}" for index, item in enumerate(items, start=1)]


def render_markdown(result: RatingResult) -> str:
    """Render one report block in the rubric's output format."""
    lines: list[str] = [
        f"# Rating: {result.filename}",
        "",
        f"**Grade:** {result.grade} ({result.average:.2f}/5.0)",
        "",
        "| # | Criterion | Wt | Score | Wtd |",
        "|---|-----------|-----|-------|-----|",
    ]
    for index, criterion in enumerate(result.criteria, start=1):
        score = result.scores.get(criterion.key, 1.0)
        lines.append(
            f"| {index} | {criterion.label} | {_num(criterion.weight)} | "
            f"{_num(score)} | {_num(score * criterion.weight)} |"
        )
    total_weight = _num(result.total_weight)
    lines.append(f"| **Total** | | **{total_weight}** | | **{result.total:.1f}** |")
    lines.append("")
    lines.append(f"**Weighted:** {result.total:.1f} / {total_weight} = {result.average:.2f}")
    lines.append("")
    lines.append("## Strengths")
    lines.extend(_numbered(result.strengths))
    lines.append("")
    lines.append("## Weaknesses")
    lines.extend(_numbered(result.weaknesses))
    lines.append("")
    lines.append("## Factual Issues")
    if result.factual_issues:
        lines.extend(f"- {issue}" for issue in result.factual_issues)
    else:
        lines.append("- None identified.")
    lines.append("")
    lines.append("## Summary")
    lines.append(result.summary or "-")
    return "\n".join(lines)


def result_to_dict(result: RatingResult) -> dict:
    """Compact per-file record for the JSON summary."""
    return {
        "slug": result.slug,
        "filename": result.filename,
        "grade": result.grade,
        "weighted": round(result.total, 2),
        "average": round(result.average, 2),
        "scores": {criterion.key: result.scores.get(criterion.key, 1.0) for criterion in result.criteria},
        "searched": result.searched,
        "warnings": result.warnings,
    }


def build_markdown(results: list[RatingResult]) -> str:
    return "\n\n---\n\n".join(render_markdown(result) for result in results)
