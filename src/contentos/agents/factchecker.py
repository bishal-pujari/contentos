"""Fact checker agent.

Validates content against the manually verified fact files in ``facts/`` (one JSON per
exam, named after the exam slug) and corrects the discrepancies that can be fixed safely.

Design:

* **Facts are the source of truth.** A fact file's ``exams`` entry carries the exam code,
  names, status, dates, price, passing score, duration, question counts, domains, allowed
  and retired services, renames and out-of-scope services, each with an official source.
* **Content is linked to exams** by its frontmatter ``examCodes`` (filled by the
  frontmatter agent) or, when absent, by detecting exam codes/names in the content using
  the fact registry itself. Content that references no exam is skipped.
* **Deterministic checks** find discrepancies in exam codes/names, service renames, retired
  and out-of-scope services, price, passing score, duration, question counts and dates.
  A finding is auto-applied only when it is safe: the exam is active, the fact is exact,
  and the occurrence is not part of a rename discussion (so a post that explains a rename
  is not rewritten). Everything else is flagged for human review.
* **Optional LLM pass** (``--llm``, default off) reads a content-type prompt and the fact
  sheet and proposes ``find -> replace`` fixes with a source; Python validates each one
  (exact match, source URL, length band) before applying. Nothing semantic is applied
  without a source.

The body is corrected; the frontmatter and the ``order`` field are never touched.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

from contentos.agents.interlinker import DEFAULT_MAX_ATTEMPTS, _invoke_json
from contentos.core.protected import count_guarded, mask, replace_guarded

DEFAULT_FACTS_DIR = "facts"
DEFAULT_PROMPT_PATH = os.path.join("prompts", "examos-content-facts.md")

_MONTHS = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]
_MONTH_NUM = {name.lower(): index for index, name in enumerate(_MONTHS, start=1)}
_MONTH_PATTERN = "|".join(_MONTHS)

ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
MDY_DATE_RE = re.compile(rf"\b({_MONTH_PATTERN})\s+(\d{{1,2}}),?\s+(\d{{4}})\b", re.IGNORECASE)
MY_DATE_RE = re.compile(rf"\b({_MONTH_PATTERN})\s+(\d{{4}})\b", re.IGNORECASE)

# A rename/predecessor discussion must not be rewritten by the auto-fixer.
DISCUSSION_RE = re.compile(
    r"renamed|formerly|previously|used to be|now (?:called|known|named)|changed to|"
    r"rebrand|name change|no longer|deprecated|retired|until\b|replaced by|replacement",
    re.IGNORECASE,
)
EXAM_CONTEXT_RE = re.compile(r"\bexam|test|certification|credential", re.IGNORECASE)

PRICE_RE = re.compile(r"(?P<dollar>\$\s?\d[\d,]*)|(?P<usd>\b\d[\d,]*\s*USD\b)", re.IGNORECASE)
PASS_SCALED_RE = re.compile(r"\b(\d{3,4})\s*/\s*(\d{3,4})\b")
PASS_PERCENT_RE = re.compile(
    r"(?:(?:pass(?:ing)?(?:\s+score|\s+mark)?|to pass)\D{0,40}?(\d{2,3})\s*%)"
    r"|(?:(\d{2,3})\s*%\s*(?:to pass|passing|pass mark))",
    re.IGNORECASE,
)
DURATION_RE = re.compile(r"\b(\d{2,3})\s*(?:minutes|min)\b", re.IGNORECASE)
QUESTION_RANGE_RE = re.compile(r"\b(\d{1,3})\s*(?:[-–]|to)\s*(\d{1,3})\s*questions\b", re.IGNORECASE)
QUESTION_EXACT_RE = re.compile(r"\b(\d{1,3})\s*questions\b", re.IGNORECASE)

MIN_TOTAL_QUESTIONS = 20
MIN_EXAM_MINUTES = 30
WINDOW = 110

DOMAIN_WEIGHT_RE = re.compile(r"(\d{1,3})\s*(?:[-–]\s*(\d{1,3}))?\s*%")
GROUP_DOMAIN_RE = re.compile(
    r"\b(two|three|four|five|six|seven|\d+)\s+(?:[\w-]+\s+){0,3}?domains?\b"
    r"[^.\n]{0,60}?(?:represent|account for|make up|cover|comprise|are|is)\s+"
    r"(?:about\s+|roughly\s+|approximately\s+|around\s+|nearly\s+)?"
    r"(\d{1,3})\s*(?:[-–]\s*(\d{1,3}))?\s*%",
    re.IGNORECASE,
)
_NUMBER_WORDS = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7}
# A competitor service near one of these is a deliberate cross-vendor comparison.
COMPARISON_RE = re.compile(
    r"\bvs\b|versus|compared|comparison|unlike|instead of|rather than|alternative|"
    r"multi-?cloud|migrat|competitor|whereas|both\s",
    re.IGNORECASE,
)

_CLOUD_MARKERS: dict[str, tuple[str, ...]] = {
    "aws": (
        r"\bAmazon S3\b", r"\bAWS Lambda\b", r"\bAmazon CloudWatch\b", r"\bCloudWatch\b",
        r"\bAmazon DynamoDB\b", r"\bDynamoDB\b", r"\bAmazon EC2\b", r"\bAmazon RDS\b",
        r"\bAmazon SQS\b", r"\bAmazon SNS\b", r"\bAmazon Bedrock\b", r"\bSageMaker\b",
        r"\bAWS Glue\b", r"\bAWS Fargate\b", r"\bAmazon Athena\b", r"\bAmazon Redshift\b",
        r"\bAWS Step Functions\b", r"\bAmazon EKS\b", r"\bAmazon ECS\b",
    ),
    "azure": (
        r"\bAzure Monitor\b", r"\bAzure Functions\b", r"\bAzure Cosmos DB\b", r"\bCosmos DB\b",
        r"\bAzure Key Vault\b", r"\bMicrosoft Entra\b", r"\bAzure Kubernetes Service\b",
        r"\bAzure Blob Storage\b", r"\bAzure SQL\b", r"\bAzure OpenAI\b", r"\bMicrosoft Foundry\b",
        r"\bAzure DevOps\b", r"\bAzure Service Bus\b", r"\bAzure Event Grid\b",
        r"\bAzure API Management\b", r"\bAzure App Service\b", r"\bAzure AI Foundry\b",
    ),
    "gcp": (
        r"\bBigQuery\b", r"\bCloud Run\b", r"\bCloud Functions\b",
        r"\bGoogle Kubernetes Engine\b", r"\bGKE\b", r"\bVertex AI\b", r"\bCloud Spanner\b",
        r"\bFirestore\b", r"\bCloud Bigtable\b", r"\bCloud Dataflow\b", r"\bCloud Composer\b",
        r"\bCloud SQL\b", r"\bCloud Storage\b",
    ),
}

_BRAND_WORD_RE = re.compile(r"\b(aws|amazon|azure|microsoft|google|gcp)\b", re.IGNORECASE)


def _brand_family(word: str) -> str:
    word = word.lower()
    if word in {"aws", "amazon"}:
        return "aws"
    if word in {"azure", "microsoft"}:
        return "azure"
    return "gcp"


@dataclass
class FactValue:
    value_type: str = "exact"
    value: object = None
    value_min: object = None
    value_max: object = None
    source_url: str = ""
    note: str = ""

    @property
    def usable(self) -> bool:
        return self.value_type in {"exact", "range"}


@dataclass
class FactExam:
    code: str
    slug: str
    official_name: str
    other_names: list[str]
    vendor: str
    status: str
    replaces: str | None
    replaced_by: str | None
    predecessor_last_date: FactValue
    ga_date_exact: FactValue
    ga_month_year: FactValue
    last_exam_update_month: FactValue
    last_day_to_take: FactValue
    passing_score: FactValue
    price_usd: FactValue
    duration_minutes: FactValue
    scored_questions: FactValue
    unscored_questions: FactValue
    domains: list[dict]
    allowed_services: list[str]
    retired_or_retiring_services: list[dict]
    out_of_scope_for_exam: list[dict]
    renames: list[dict]
    sources: list[dict]
    facts_path: str
    field_sources: dict[str, str] = field(default_factory=dict)

    @property
    def default_source(self) -> str:
        for source in self.sources:
            if source.get("official") and source.get("url"):
                return str(source["url"])
        for source in self.sources:
            if source.get("url"):
                return str(source["url"])
        return ""

    def source_for(self, field_name: str) -> str:
        return self.field_sources.get(field_name) or self.default_source


@dataclass
class FactFinding:
    category: str
    field: str
    find: str
    replacement: str
    reason: str
    source: str
    applied: bool = False
    case_insensitive: bool = False

    def to_dict(self) -> dict:
        return {
            "category": self.category,
            "field": self.field,
            "find": self.find,
            "replace": self.replacement,
            "reason": self.reason,
            "source": self.source,
            "applied": self.applied,
        }


@dataclass
class FactCheckResult:
    slug: str
    filename: str
    new_text: str
    exams: list[str] = field(default_factory=list)
    findings: list[dict] = field(default_factory=list)
    review_items: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    searched: bool = False


# --- Loading ------------------------------------------------------------------


def _fact_value(payload: object) -> FactValue:
    if not isinstance(payload, dict):
        return FactValue(value_type="unpublished")
    return FactValue(
        value_type=str(payload.get("value_type", "exact")),
        value=payload.get("value"),
        value_min=payload.get("value_min"),
        value_max=payload.get("value_max"),
        source_url=str(payload.get("source_url") or ""),
        note=str(payload.get("note") or ""),
    )


def _fact_exam(payload: dict, path: str) -> FactExam:
    field_sources: dict[str, str] = {}
    for source in payload.get("sources") or []:
        url = str(source.get("url") or "")
        for covered in source.get("covers") or []:
            field_sources.setdefault(str(covered), url)
    return FactExam(
        code=str(payload.get("code") or ""),
        slug=str(payload.get("slug") or ""),
        official_name=str(payload.get("official_name") or ""),
        other_names=[str(name) for name in (payload.get("other_names") or [])],
        vendor=str(payload.get("vendor") or ""),
        status=str(payload.get("status") or "active"),
        replaces=payload.get("replaces"),
        replaced_by=payload.get("replaced_by"),
        predecessor_last_date=_fact_value(payload.get("predecessor_last_date")),
        ga_date_exact=_fact_value(payload.get("ga_date_exact")),
        ga_month_year=_fact_value(payload.get("ga_month_year")),
        last_exam_update_month=_fact_value(payload.get("last_exam_update_month")),
        last_day_to_take=_fact_value(payload.get("last_day_to_take")),
        passing_score=_fact_value(payload.get("passing_score")),
        price_usd=_fact_value(payload.get("price_usd")),
        duration_minutes=_fact_value(payload.get("duration_minutes")),
        scored_questions=_fact_value(payload.get("scored_questions")),
        unscored_questions=_fact_value(payload.get("unscored_questions")),
        domains=list(payload.get("domains") or []),
        allowed_services=[str(name) for name in (payload.get("allowed_services") or [])],
        retired_or_retiring_services=list(payload.get("retired_or_retiring_services") or []),
        out_of_scope_for_exam=list(payload.get("out_of_scope_for_exam") or []),
        renames=list(payload.get("renames") or []),
        sources=list(payload.get("sources") or []),
        facts_path=path,
        field_sources=field_sources,
    )


def load_fact_registry(facts_dir: str = DEFAULT_FACTS_DIR) -> list[FactExam]:
    """Load every exam entry from every ``*.json`` file under ``facts_dir``."""
    if not facts_dir or not os.path.isdir(facts_dir):
        return []
    facts: list[FactExam] = []
    for name in sorted(os.listdir(facts_dir)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(facts_dir, name)
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            continue
        for payload in data.get("exams") or []:
            if isinstance(payload, dict) and payload.get("code"):
                facts.append(_fact_exam(payload, path))
    return facts


def load_fact_prompt(path: str = DEFAULT_PROMPT_PATH) -> str:
    """Load the content-type fact-check prompt body (YAML frontmatter stripped)."""
    from contentos.core.loaders import load_document

    if not path or not os.path.isfile(path):
        return ""
    return load_document(path).body.strip()


class FactRegistry:
    """Fact exams indexed by code, with a fallback name/code detector."""

    def __init__(self, facts: list[FactExam]) -> None:
        self.facts = facts
        self.by_code: dict[str, FactExam] = {}
        self.by_slug: dict[str, FactExam] = {}
        for fact in facts:
            self.by_code.setdefault(fact.code.upper(), fact)
            if fact.slug:
                self.by_slug.setdefault(fact.slug.lower(), fact)

    def for_code(self, code: str) -> FactExam | None:
        return self.by_code.get(str(code).strip().upper())

    def for_slug(self, slug: str) -> FactExam | None:
        return self.by_slug.get(str(slug).strip().lower())


# --- Linking content to exams -------------------------------------------------


def _norm(value: object) -> str:
    text = str(value).lower().replace("+", " plus ").replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _code_pattern(code: str) -> re.Pattern[str]:
    parts = [part for part in re.split(r"[-_ ]+", code) if part]
    core = r"[-_ ]?".join(re.escape(part) for part in parts)
    return re.compile(rf"(?<![a-z0-9]){core}(?![a-z0-9])", re.IGNORECASE)


def _alias_pattern(alias: str) -> re.Pattern[str]:
    """A name alias as a regex that tolerates any separator between its words."""
    parts = [re.escape(part) for part in alias.split()]
    return re.compile(r"(?<![a-z0-9])" + r"[\s\W_]+".join(parts) + r"(?![a-z0-9])", re.IGNORECASE)


_VENDOR_PREFIXES = {
    "microsoft",
    "aws",
    "amazon",
    "comptia",
    "google",
    "isaca",
    "pmi",
    "cisco",
    "hashicorp",
    "claude",
    "anthropic",
    "scaled",
}

# Words too generic to identify an exam on their own. A vendor-stripped alias made only
# of these ("developer associate", "cloud practitioner") is dropped: a role phrase in a
# slug must not bind an exam (e.g. "...-developer-associate-ai-103" -> DVA-C02).
_GENERIC_NAME_WORDS = {
    "certified", "certification", "associate", "professional", "expert", "specialist",
    "administrator", "developer", "engineer", "architect", "analyst", "fundamentals",
    "practitioner", "solutions", "cloud", "data", "network", "security", "ai", "and",
    "the", "for", "with",
} | _VENDOR_PREFIXES


def _name_aliases(fact: FactExam) -> set[str]:
    """Exam names worth matching (official name and other names; never service renames).

    A leading vendor prefix and the words "certified"/"certification" are stripped so the
    common name matches ("AWS Certified Cloud Practitioner" -> "cloud practitioner"), but a
    stripped alias made only of generic role words is dropped so a role phrase cannot bind
    the wrong exam. Names with a ``+`` stay conservative so "Security + Data" is not read
    as Security+.
    """
    aliases: set[str] = set()
    for candidate in [fact.official_name, *fact.other_names]:
        norm = _norm(candidate)
        if not norm:
            continue
        tokens = norm.split()
        variants = {" ".join(tokens)}
        if "+" not in str(candidate):
            core = list(tokens)
            while core and core[0] in _VENDOR_PREFIXES:
                core.pop(0)
            core = [token for token in core if token not in {"certified", "certification"}]
            if any(token not in _GENERIC_NAME_WORDS for token in core):
                variants.add(" ".join(core))
        for alias in variants:
            if len(alias) >= 8 and len(alias.split()) >= 2:
                aliases.add(alias)
    return aliases


def _full_name_aliases(fact: FactExam) -> set[str]:
    """Official/other names only, with no vendor-stripped variant (strict matching)."""
    aliases: set[str] = set()
    for candidate in [fact.official_name, *fact.other_names]:
        norm = _norm(candidate)
        if norm and len(norm) >= 8 and len(norm.split()) >= 2:
            aliases.add(norm)
    return aliases


def _mention_spans(fact: FactExam, text: str, *, strict: bool = False) -> list[tuple[int, int]]:
    spans = [(m.start(), m.end()) for m in _code_pattern(fact.code).finditer(text)]
    names = _full_name_aliases(fact) if strict else _name_aliases(fact)
    for alias in names:
        spans.extend((m.start(), m.end()) for m in _alias_pattern(alias).finditer(text))
    return spans


def _mentions(fact: FactExam, text: str, *, strict: bool = False) -> bool:
    return bool(_mention_spans(fact, text, strict=strict))


def _subject_near(
    text: str, start: int, end: int, exams: list[FactExam], radius: int = 70
) -> FactExam | None:
    """The exam whose mention is nearest a span (within ``radius``), or ``None``."""
    nearest: FactExam | None = None
    nearest_distance = radius + 1
    for fact in exams:
        for span_start, span_end in _mention_spans(fact, text):
            distance = 0 if (start <= span_end and end >= span_start) else min(
                abs(span_start - end), abs(start - span_end)
            )
            if distance < nearest_distance:
                nearest_distance = distance
                nearest = fact
    return nearest


_CODE_FIELDS = ("examCodes", "examCode", "exam_id")


def _frontmatter_codes(frontmatter: str, field: str) -> list[str]:
    """Values of a frontmatter field: inline list or scalar (``examCodes``/``examCode``)."""
    match = re.search(rf"(?m)^{re.escape(field)}:\s*(.*)$", frontmatter)
    if not match:
        return []
    raw = match.group(1).strip()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    return [item.strip().strip("\"'") for item in raw.split(",") if item.strip()]


def link_exams(
    doc,
    slug: str,
    registry: FactRegistry,
    limit: int = 3,
    *,
    exam_binding: tuple[str, ...] | None = None,
    path: str | None = None,
    unresolved: list[str] | None = None,
) -> tuple[list[FactExam], set[str]]:
    """Return ``(exams, primary_codes)`` for a document.

    A frontmatter exam code (``examCodes`` / ``examCode`` / ``exam_id``) is
    **authoritative**: when any of these fields is present it is read whatever the profile,
    and it alone binds the document. If it names an exam the fact registry does not know,
    nothing is bound and the code is reported through ``unresolved`` (the body is never
    mined to guess a different exam). When no code field exists, the profile's
    ``exam_binding`` sources (``filename_slug`` / ``exam_folder`` / ``categories``) are
    tried. Only as a last resort are exams detected from metadata (strict: codes and full
    names only) or the body, and those are **secondary** — never auto-fixed.
    """
    binding = tuple(exam_binding) if exam_binding else _CODE_FIELDS
    matched: list[FactExam] = []
    primary: set[str] = set()

    def _add(fact: FactExam | None, *, is_primary: bool) -> None:
        if fact is not None and fact not in matched:
            matched.append(fact)
            if is_primary:
                primary.add(fact.code)

    if any(field in binding for field in _CODE_FIELDS):
        raw_codes: list[str] = []
        for field_name in _CODE_FIELDS:
            raw_codes.extend(_frontmatter_codes(doc.frontmatter, field_name))
        for code in raw_codes:
            _add(registry.for_code(code), is_primary=True)
        if raw_codes and not matched:
            if unresolved is not None:
                unresolved.extend(raw_codes)
            return [], set()

    if "filename_slug" in binding and slug:
        _add(registry.for_slug(slug), is_primary=True)
    if "exam_folder" in binding and path:
        folder = os.path.basename(os.path.dirname(os.path.normpath(path)))
        _add(registry.for_slug(folder), is_primary=True)
        _add(registry.for_code(folder), is_primary=True)
    if "categories" in binding:
        for category in _frontmatter_codes(doc.frontmatter, "categories"):
            _add(registry.for_code(category), is_primary=True)
            _add(registry.for_slug(category), is_primary=True)
    if matched:
        return matched[:limit], primary

    meta = f"{slug}\n{doc.frontmatter}"
    meta_hits = [fact for fact in registry.facts if _mentions(fact, meta, strict=True)]
    if meta_hits:
        return meta_hits[:limit], set()
    body = mask(doc.body)
    body_hits = [fact for fact in registry.facts if _mentions(fact, body)]
    if body_hits:
        body_hits = body_hits[:limit]
    return body_hits, set()


# --- Finding helpers ----------------------------------------------------------


def _window(text: str, start: int, end: int, radius: int = WINDOW) -> str:
    return text[max(0, start - radius) : min(len(text), end + radius)]


def _boundary_pattern(find: str) -> re.Pattern[str]:
    left = r"\b" if find[:1].isalnum() else ""
    right = r"\b" if find[-1:].isalnum() else ""
    return re.compile(f"{left}{re.escape(find)}{right}")


def _replace(text: str, finding: FactFinding) -> str:
    return replace_guarded(
        text,
        finding.find,
        finding.replacement,
        case_insensitive=finding.case_insensitive,
    )


def _int(value: object) -> int | None:
    try:
        return int(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _is_range_member(text: str, start: int, lookback: int = 6) -> bool:
    """True when a numeric token is the upper bound of a range ("40-60 questions")."""
    prefix = text[max(0, start - lookback) : start]
    return bool(re.search(r"(?:[-–—]|\bto|\band)\s*$", prefix, re.IGNORECASE))


# --- Checks -------------------------------------------------------------------


def _rename_conflicts(exams: list[FactExam]) -> set[str]:
    """Old names that different exams rename to different targets."""
    targets: dict[str, set[str]] = {}
    for fact in exams:
        for old, new in _resolve_renames(fact).items():
            targets.setdefault(old, set()).add(new)
    return {old for old, values in targets.items() if len(values) > 1}


def _check_renames(
    fact: FactExam,
    body: str,
    findings: list[FactFinding],
    conflicts: set[str],
    subjects: list[FactExam],
    is_primary: bool,
    single_primary: bool,
) -> None:
    terminal = _resolve_renames(fact)
    source = fact.source_for("renames")
    for old, new in terminal.items():
        if len(old) < 4:
            continue
        pattern = re.compile(re.escape(old), re.IGNORECASE)
        matches = list(pattern.finditer(body))
        if not matches:
            continue
        blocked = any(DISCUSSION_RE.search(_window(body, m.start(), m.end())) for m in matches)
        note = " (appears in a rename discussion; confirm before editing)" if blocked else ""
        if old in conflicts:
            blocked = True
            note = " (different exams give this name different successors; confirm before editing)"
        if not single_primary and any(
            _subject_near(body, m.start(), m.end(), subjects, radius=40) is not fact
            for m in matches
        ):
            blocked = True
            note = " (not clearly tied to this exam; confirm before editing)"
        if not is_primary:
            blocked = True
            note = note or " (exam detected from the body only; confirm before editing)"
        if fact.status != "active":
            blocked = True
            note = note or " (this exam is retired; confirm before editing)"
        findings.append(
            FactFinding(
                category="rename",
                field="renames",
                find=old,
                replacement=new,
                reason=f"renamed to '{new}'{note}",
                source=source,
                applied=not blocked,
                case_insensitive=True,
            )
        )


def _resolve_renames(fact: FactExam) -> dict[str, str]:
    """Map each old name to the terminal new name, following rename chains."""
    table: dict[str, str] = {}
    for rename in fact.renames:
        old = str(rename.get("from") or "").strip()
        new = str(rename.get("to") or "").strip()
        for variant in re.split(r"[/,]", old):
            variant = variant.strip()
            if variant:
                table[variant] = new
    resolved: dict[str, str] = {}
    for variant, new in table.items():
        seen = {variant}
        current = new
        while current in table and current not in seen:
            seen.add(current)
            current = table[current]
        resolved[variant] = current
    return resolved


def _check_services(fact: FactExam, body: str, findings: list[FactFinding]) -> None:
    for service in fact.retired_or_retiring_services:
        name = str(service.get("name") or "")
        base = name.split(" (")[0].strip()
        if not base or not _boundary_pattern(base).search(body):
            continue
        status = str(service.get("status") or "retiring")
        successor = service.get("successor")
        reason = f"'{base}' is {status}"
        if service.get("retirement_date"):
            reason += f" ({service['retirement_date']})"
        if successor:
            reason += f"; successor: {successor}"
        findings.append(
            FactFinding(
                category="retired_service",
                field="retired_or_retiring_services",
                find=base,
                replacement=str(successor or ""),
                reason=reason,
                source=str(service.get("source_url") or fact.default_source),
                applied=False,
            )
        )

    for service in fact.out_of_scope_for_exam:
        name = str(service.get("name") or "")
        base = name.split(" (")[0].strip()
        if not base or not _boundary_pattern(base).search(body):
            continue
        findings.append(
            FactFinding(
                category="out_of_scope",
                field="out_of_scope_for_exam",
                find=base,
                replacement="",
                reason=f"'{base}' is out of scope for {fact.code}: {service.get('reason') or ''}".strip(),
                source=str(service.get("source_url") or fact.default_source),
                applied=False,
            )
        )


def _claim_belongs(
    text: str,
    start: int,
    end: int,
    fact: FactExam,
    subjects: list[FactExam],
    only_exam: bool,
) -> bool:
    """True when a span is about ``fact``: its nearest exam is ``fact``, or it is the
    only exam the whole document mentions."""
    subject = _subject_near(text, start, end, subjects)
    if subject is None:
        return only_exam
    return subject is fact


_FEE_WORDS = re.compile(r"\bexam|fee|cost|price|register|voucher|USD\b", re.IGNORECASE)
MAX_EXAM_PRICE = 1000


def _check_values(
    fact: FactExam,
    body: str,
    findings: list[FactFinding],
    subjects: list[FactExam],
    only_exam: bool,
    allow_fix: bool,
) -> None:
    if fact.price_usd.value_type == "exact":
        expected = _int(fact.price_usd.value)
        if expected is not None and expected <= MAX_EXAM_PRICE:
            for match in PRICE_RE.finditer(body):
                claimed = _int(re.sub(r"[^\d,]", "", match.group(0)))
                if claimed is None or claimed == expected or claimed > MAX_EXAM_PRICE:
                    continue
                if _is_range_member(body, match.start()):
                    continue
                window = _window(body, match.start(), match.end(), radius=45)
                if not _FEE_WORDS.search(window):
                    continue
                adjacent = _subject_near(body, match.start(), match.end(), subjects, radius=45) is fact
                if not _claim_belongs(body, match.start(), match.end(), fact, subjects, only_exam):
                    continue
                style = match.group(0)
                replacement = f"${expected}" if "$" in style else f"{expected} USD"
                findings.append(
                    _value_finding(
                        "price", "price_usd", style, replacement,
                        f"exam fee is ${expected} USD", fact, allow_fix and adjacent,
                    )
                )

    if fact.passing_score.value_type == "exact":
        expected = _int(fact.passing_score.value)
        if expected is not None and expected > 100:
            for match in PASS_SCALED_RE.finditer(body):
                if not _claim_belongs(body, match.start(), match.end(), fact, subjects, only_exam):
                    continue
                claimed = _int(match.group(1))
                if claimed is None or claimed == expected:
                    continue
                findings.append(
                    _value_finding(
                        "passing_score", "passing_score", match.group(0),
                        f"{expected}/{match.group(2)}",
                        f"passing score is {expected}/{match.group(2)}", fact, allow_fix,
                    )
                )
        elif expected is not None and expected <= 100:
            for match in PASS_PERCENT_RE.finditer(body):
                if not _claim_belongs(body, match.start(), match.end(), fact, subjects, only_exam):
                    continue
                token = match.group(0)
                claimed = _int(re.search(r"\d+", token).group(0))
                if claimed is None or claimed == expected:
                    continue
                findings.append(
                    _value_finding(
                        "passing_score", "passing_score", token,
                        token.replace(str(claimed), str(expected)),
                        f"passing score is {expected}%", fact, allow_fix,
                    )
                )

    if fact.duration_minutes.value_type == "exact":
        expected = _int(fact.duration_minutes.value)
        if expected is not None:
            for match in DURATION_RE.finditer(body):
                claimed = _int(match.group(1))
                if claimed is None or claimed == expected or claimed < MIN_EXAM_MINUTES:
                    continue
                if _is_range_member(body, match.start()):
                    continue
                window = _window(body, match.start(), match.end())
                if not EXAM_CONTEXT_RE.search(window):
                    continue
                if not _claim_belongs(body, match.start(), match.end(), fact, subjects, only_exam):
                    continue
                findings.append(
                    _value_finding(
                        "duration", "duration_minutes", match.group(0),
                        match.group(0).replace(match.group(1), str(expected)),
                        f"exam duration is {expected} minutes", fact, allow_fix,
                    )
                )

    _check_questions(fact, body, findings, subjects, only_exam, allow_fix)


def _check_questions(
    fact: FactExam,
    body: str,
    findings: list[FactFinding],
    subjects: list[FactExam],
    only_exam: bool,
    allow_fix: bool,
) -> None:
    fact_value = fact.scored_questions
    if not fact_value.usable:
        return
    exact = _int(fact_value.value) if fact_value.value_type == "exact" else None
    low = _int(fact_value.value_min)
    high = _int(fact_value.value_max)
    if exact:
        low = high = exact

    for match in QUESTION_RANGE_RE.finditer(body):
        low_claim = _int(match.group(1))
        high_claim = _int(match.group(2))
        if low_claim is None or low_claim < MIN_TOTAL_QUESTIONS:
            continue
        if not _claim_belongs(body, match.start(), match.end(), fact, subjects, only_exam):
            continue
        if low is not None and high is not None and low <= low_claim and high_claim <= high:
            continue
        findings.append(
            FactFinding(
                category="questions",
                field="scored_questions",
                find=match.group(0),
                replacement=_format_question_count(fact_value),
                reason=f"scored questions: {_format_question_count(fact_value)}",
                source=fact.source_for("scored_questions"),
                applied=False,
            )
        )

    if not exact:
        return
    for match in QUESTION_EXACT_RE.finditer(body):
        claimed = _int(match.group(1))
        if claimed is None or claimed == exact or claimed < MIN_TOTAL_QUESTIONS:
            continue
        if _is_range_member(body, match.start()):
            continue
        if not _claim_belongs(body, match.start(), match.end(), fact, subjects, only_exam):
            continue
        findings.append(
            _value_finding(
                "questions", "scored_questions", match.group(0),
                match.group(0).replace(match.group(1), str(exact)),
                f"scored questions: {exact}", fact, allow_fix,
            )
        )


def _format_question_count(fact_value: FactValue) -> str:
    if fact_value.value_type == "range":
        return f"{fact_value.value_min}-{fact_value.value_max} questions"
    return f"{fact_value.value} questions"


def _parse_weight_range(value: object) -> tuple[int, int] | None:
    if value is None:
        return None
    numbers = [int(number) for number in re.findall(r"\d+", str(value))]
    if not numbers:
        return None
    return numbers[0], numbers[-1]


def _overlaps(first: tuple[int, int], second: tuple[int, int]) -> bool:
    return max(first[0], second[0]) <= min(first[1], second[1])


def _nearest_weight(text: str, start: int, end: int, radius: int = 90) -> re.Match | None:
    """The weight token nearest a domain-name span (its own table row, typically).

    A symmetric window can span neighbouring rows, so the match closest to the name is
    used rather than the first one: ``| Monitor ... | 10-15% |`` must read 10-15, not the
    20-25 of the row above.
    """
    window_start = max(0, start - radius)
    window_end = min(len(text), end + radius)
    best: re.Match | None = None
    best_distance: int | None = None
    for weight in DOMAIN_WEIGHT_RE.finditer(text, window_start, window_end):
        if start <= weight.end() and end >= weight.start():
            distance = 0
        else:
            distance = min(abs(weight.start() - end), abs(start - weight.end()))
        if best_distance is None or distance < best_distance:
            best, best_distance = weight, distance
    return best


def _check_domains(
    fact: FactExam,
    body: str,
    findings: list[FactFinding],
    subjects: list[FactExam],
    only_exam: bool,
) -> None:
    for domain in fact.domains:
        name = str(domain.get("name") or "")
        fact_range = _parse_weight_range(domain.get("weight_percent"))
        if len(_norm(name)) < 5 or fact_range is None:
            continue
        pattern = _alias_pattern(_norm(name))
        for match in pattern.finditer(body):
            if not _claim_belongs(body, match.start(), match.end(), fact, subjects, only_exam):
                break
            weight = _nearest_weight(body, match.start(), match.end())
            if weight is None:
                continue
            claimed = (int(weight.group(1)), int(weight.group(2) or weight.group(1)))
            if _overlaps(claimed, fact_range):
                break
            findings.append(
                FactFinding(
                    category="domain_weight",
                    field="domains",
                    find=name,
                    replacement=str(domain.get("weight_percent") or ""),
                    reason=(
                        f"'{name}' is {domain.get('weight_percent')}% on the fact sheet, "
                        f"not {claimed[0]}-{claimed[1]}%"
                    ),
                    source=fact.source_for("domains"),
                    applied=False,
                )
            )
            break


def _check_grouped_domain_math(
    fact: FactExam,
    body: str,
    findings: list[FactFinding],
    subjects: list[FactExam],
    only_exam: bool,
) -> None:
    ranges = [r for r in (_parse_weight_range(d.get("weight_percent")) for d in fact.domains) if r]
    if not ranges:
        return
    for match in GROUP_DOMAIN_RE.finditer(body):
        if not _claim_belongs(body, match.start(), match.end(), fact, subjects, only_exam):
            continue
        token = match.group(1).lower()
        count = _NUMBER_WORDS.get(token)
        if count is None:
            count = int(token)
        lo, hi = int(match.group(2)), int(match.group(3) or match.group(2))
        if count < 1 or count > len(ranges):
            findings.append(
                FactFinding(
                    category="domain_weight_math",
                    field="domains",
                    find=match.group(0),
                    replacement="",
                    reason=(
                        f"the fact sheet has {len(ranges)} domains, but the content groups "
                        f"{count} of them"
                    ),
                    source=fact.source_for("domains"),
                    applied=False,
                )
            )
            continue
        possible_lo = sum(sorted(r[0] for r in ranges)[:count])
        possible_hi = sum(sorted((r[1] for r in ranges), reverse=True)[:count])
        if hi < possible_lo or lo > possible_hi:
            findings.append(
                FactFinding(
                    category="domain_weight_math",
                    field="domains",
                    find=match.group(0),
                    replacement="",
                    reason=(
                        f"no group of {count} domains totals {lo}-{hi}% on the fact sheet "
                        f"(possible {possible_lo}-{possible_hi}%)"
                    ),
                    source=fact.source_for("domains"),
                    applied=False,
                )
            )


def _vendor_cloud(vendor: object) -> str | None:
    text = str(vendor).lower()
    if "microsoft" in text or "azure" in text:
        return "azure"
    if "amazon" in text or "aws" in text:
        return "aws"
    if "google" in text:
        return "gcp"
    return None


def _check_vendor_contamination(
    fact: FactExam, body: str, findings: list[FactFinding], primary_exams: list[FactExam]
) -> None:
    home = _vendor_cloud(fact.vendor)
    if home is None:
        return
    clouds = {_vendor_cloud(other.vendor) for other in primary_exams}
    clouds.discard(None)
    if len(clouds) > 1:
        return  # a deliberate multi-cloud comparison post
    seen: set[str] = set()
    for cloud, patterns in _CLOUD_MARKERS.items():
        if cloud == home:
            continue
        for pattern in patterns:
            for match in re.finditer(pattern, body, re.IGNORECASE):
                token = match.group(0)
                if token.lower() in seen:
                    continue
                window = _window(body, match.start(), match.end(), radius=90)
                if COMPARISON_RE.search(window):
                    continue
                brands = {_brand_family(word) for word in _BRAND_WORD_RE.findall(window)}
                if len(brands) > 1:
                    continue  # the window names several clouds (a list/comparison)
                seen.add(token.lower())
                findings.append(
                    FactFinding(
                        category="vendor_contamination",
                        field="allowed_services",
                        find=token,
                        replacement="",
                        reason=(
                            f"'{token}' is a {cloud.upper()} service; this is a "
                            f"{home.upper()}-vendor exam, so use the vendor's native service"
                        ),
                        source=fact.default_source,
                        applied=False,
                    )
                )


def _flag_retired_status(fact: FactExam, body: str, findings: list[FactFinding]) -> None:
    if not _mentions(fact, body):
        return
    if DISCUSSION_RE.search(body):
        return
    reason = f"{fact.code} is listed as retired"
    if fact.last_day_to_take.value:
        reason += f" (last day to take: {fact.last_day_to_take.value})"
    if fact.replaced_by:
        reason += f"; replaced by {fact.replaced_by}"
    findings.append(
        FactFinding(
            category="status",
            field="status",
            find=fact.code,
            replacement="",
            reason=reason,
            source=fact.source_for("status"),
            applied=False,
        )
    )


def _value_finding(
    category: str,
    field_name: str,
    find: str,
    replacement: str,
    reason: str,
    fact: FactExam,
    allow_fix: bool,
) -> FactFinding:
    return FactFinding(
        category=category,
        field=field_name,
        find=find,
        replacement=replacement,
        reason=reason,
        source=fact.source_for(field_name),
        applied=allow_fix and replacement != find,
    )


_RETIRE_WORDS = re.compile(
    r"retire|retired|retirement|last day|discontinued|until\b", re.IGNORECASE
)
_GA_WORDS = re.compile(
    r"generally available|general availability|went ga|became ga|\bga\b", re.IGNORECASE
)


def _check_dates(
    fact: FactExam,
    body: str,
    findings: list[FactFinding],
    subjects: list[FactExam],
    only_exam: bool,
    allow_fix: bool,
) -> None:
    active = allow_fix

    last_day = _parse_iso(str(fact.last_day_to_take.value)) if fact.last_day_to_take.value_type == "exact" else None

    if fact.predecessor_last_date.value_type == "exact" and fact.replaces:
        predecessor = str(fact.replaces)
        predecessor_re = _code_pattern(predecessor)
        for start, end, parsed in _date_claims(body):
            preceding = body[max(0, start - 60) : start]
            if not predecessor_re.search(preceding) or not _RETIRE_WORDS.search(preceding):
                continue
            expected = _parse_iso(str(fact.predecessor_last_date.value))
            if expected is None or parsed == expected:
                continue
            findings.append(
                _date_finding(
                    body, start, end, str(fact.predecessor_last_date.value),
                    f"{predecessor} last day was {fact.predecessor_last_date.value}",
                    fact, active,
                )
            )

    if fact.ga_date_exact.value_type == "exact":
        for start, end, parsed in _date_claims(body):
            # A date equal to the last day to take marks a retirement boundary, not a GA.
            if last_day is not None and parsed == last_day:
                continue
            if not _claim_belongs(body, start, end, fact, subjects, only_exam):
                continue
            window = _window(body, start, end, radius=60)
            if not _GA_WORDS.search(window):
                continue
            expected = _parse_iso(str(fact.ga_date_exact.value))
            if expected is None or parsed == expected:
                continue
            findings.append(
                _date_finding(
                    body, start, end, str(fact.ga_date_exact.value),
                    f"{fact.code} went GA on {fact.ga_date_exact.value}", fact, active,
                )
            )

    if fact.last_day_to_take.value_type == "exact":
        for start, end, parsed in _date_claims(body):
            preceding = body[max(0, start - 60) : start]
            if not (_mentions(fact, preceding) or _code_pattern(fact.code).search(preceding)):
                continue
            if not _RETIRE_WORDS.search(preceding):
                continue
            expected = _parse_iso(str(fact.last_day_to_take.value))
            if expected is None or parsed == expected:
                continue
            findings.append(
                _date_finding(
                    body, start, end, str(fact.last_day_to_take.value),
                    f"{fact.code} last day to take is {fact.last_day_to_take.value}", fact, active,
                )
            )


def _date_finding(
    body: str, start: int, end: int, iso: str, reason: str, fact: FactExam, active: bool
) -> FactFinding:
    claim = body[start:end]
    replacement = _format_date(iso, iso_style="-" in claim)
    return FactFinding(
        category="date",
        field="date",
        find=claim,
        replacement=replacement,
        reason=reason,
        source=fact.default_source,
        applied=active,
    )


def _date_claims(body: str) -> list[tuple[int, int, tuple[int, int, int] | None]]:
    claims: list[tuple[int, int, tuple[int, int, int] | None]] = []
    taken: list[tuple[int, int]] = []
    for pattern, parser in (
        (ISO_DATE_RE, _parse_iso),
        (MDY_DATE_RE, _parse_mdy),
        (MY_DATE_RE, _parse_my),
    ):
        for match in pattern.finditer(body):
            if any(match.start() < t_end and match.end() > t_start for t_start, t_end in taken):
                continue
            taken.append((match.start(), match.end()))
            claims.append((match.start(), match.end(), parser(match.group(0))))
    return claims


def _parse_iso(text: str) -> tuple[int, int, int] | None:
    match = ISO_DATE_RE.search(text)
    if match:
        return int(match.group(1)), int(match.group(2)), int(match.group(3))
    return None


def _parse_mdy(text: str) -> tuple[int, int, int] | None:
    match = MDY_DATE_RE.search(text)
    if match:
        return int(match.group(3)), _MONTH_NUM.get(match.group(1).lower(), 1), int(match.group(2))
    return None


def _parse_my(text: str) -> tuple[int, int, int] | None:
    match = MY_DATE_RE.search(text)
    if match:
        return int(match.group(2)), _MONTH_NUM.get(match.group(1).lower(), 1), 1
    return None


def _format_date(iso: str, iso_style: bool) -> str:
    parts = iso.split("-")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return iso
    year, month, day = parts
    if iso_style:
        return iso
    return f"{_MONTHS[int(month) - 1]} {int(day)}, {year}"


# --- LLM pass (optional) ------------------------------------------------------


FACTCHECK_CONTRACT = """## OUTPUT (FACT CHECK TASK)
Compare the content with the verified fact sheet. Report only concrete discrepancies that
the fact sheet proves wrong: dates, prices, passing scores, question counts, durations,
service/product names, exam status, domain names and weights (including the count and sum
of a grouped-domain claim), and cross-cloud vendor contamination (a competitor service
used for an exam vendor's own service). For each fix, copy the `find` text EXACTLY as it
appears in the content. Every fix needs an official source URL. Never invent facts.
Return ONLY a JSON object. No markdown, no prose, no code fence:
{
  "issues": [
    {"find": "the exact wrong text",
     "replace": "the corrected text",
     "reason": "what was wrong",
     "source": "https://official-source-url"}
  ],
  "unresolved": ["anything you are confident is wrong but cannot fix from the fact sheet"]
}
Return an empty issues list when there is no discrepancy. If the fact sheet does not cover
a claim, list it under unresolved instead of guessing."""

FACT_MAX_GROWTH = 2.0
FACT_MIN_SHRINK = 0.5


def build_fact_prompt(fact: FactExam, doc, slug: str) -> str:
    return (
        f"SOURCE SLUG: {slug}\n"
        f"FILENAME: {os.path.basename(doc.path)}\n"
        f"EXAM: {fact.code} - {fact.official_name} ({fact.vendor})\n\n"
        f"VERIFIED FACT SHEET (JSON):\n{json.dumps(fact_to_dict(fact), indent=2)}\n\n"
        f"CONTENT BODY:\n{doc.body}\n"
    )


def generate_fact_llm(fact: FactExam, doc, slug: str, llm, prompt: str, max_attempts: int, on_retry=None) -> dict:
    from langchain_core.messages import HumanMessage, SystemMessage

    messages = [
        SystemMessage(content=f"{prompt}\n\n{FACTCHECK_CONTRACT}"),
        HumanMessage(content=build_fact_prompt(fact, doc, slug)),
    ]
    data = _invoke_json(llm, messages, max_attempts=max_attempts, on_retry=on_retry)
    return data if isinstance(data, dict) else {}


def fact_to_dict(fact: FactExam) -> dict:
    """A compact, JSON-friendly view of the fact exam for the LLM prompt."""
    return {
        "code": fact.code,
        "official_name": fact.official_name,
        "other_names": fact.other_names,
        "vendor": fact.vendor,
        "status": fact.status,
        "replaces": fact.replaces,
        "replaced_by": fact.replaced_by,
        "predecessor_last_date": _fact_value_dict(fact.predecessor_last_date),
        "ga_date_exact": _fact_value_dict(fact.ga_date_exact),
        "last_day_to_take": _fact_value_dict(fact.last_day_to_take),
        "passing_score": _fact_value_dict(fact.passing_score),
        "price_usd": _fact_value_dict(fact.price_usd),
        "duration_minutes": _fact_value_dict(fact.duration_minutes),
        "scored_questions": _fact_value_dict(fact.scored_questions),
        "domains": fact.domains,
        "allowed_services": fact.allowed_services,
        "retired_or_retiring_services": fact.retired_or_retiring_services,
        "out_of_scope_for_exam": fact.out_of_scope_for_exam,
        "renames": fact.renames,
    }


def _fact_value_dict(value: FactValue) -> dict:
    payload = {"value_type": value.value_type, "note": value.note, "source_url": value.source_url}
    if value.value_type == "range":
        payload["value_min"] = value.value_min
        payload["value_max"] = value.value_max
    else:
        payload["value"] = value.value
    return payload


def _apply_llm_issues(body: str, data: dict, result: FactCheckResult) -> str:
    for issue in data.get("issues") if isinstance(data.get("issues"), list) else []:
        if not isinstance(issue, dict):
            continue
        find = str(issue.get("find") or "")
        replace = str(issue.get("replace") or "")
        source = str(issue.get("source") or "").strip()
        if not find or not replace or find == replace or count_guarded(body, find) == 0:
            continue
        if not source.startswith("http"):
            result.warnings.append(f"LLM fix skipped (no source): {find[:50]}")
            continue
        ratio = len(replace) / len(find) if find else 0
        if not (FACT_MIN_SHRINK <= ratio <= FACT_MAX_GROWTH):
            result.warnings.append(f"LLM fix skipped (length changed too much): {find[:50]}")
            continue
        body = replace_guarded(body, find, replace)
        result.sources.append(source)
        result.findings.append(
            {
                "category": "llm",
                "field": "content",
                "find": find,
                "replace": replace,
                "reason": str(issue.get("reason") or "LLM fact fix"),
                "source": source,
                "applied": True,
            }
        )
    for unresolved in data.get("unresolved") if isinstance(data.get("unresolved"), list) else []:
        text = str(unresolved).strip()
        if text:
            result.review_items.append(f"unresolved: {text}")
    return body


# --- Orchestration ------------------------------------------------------------


def generate_factcheck(
    doc,
    slug: str,
    registry: FactRegistry,
    *,
    fix: bool = True,
    llm=None,
    prompt: str = "",
    exam_binding: tuple[str, ...] | None = None,
    vendor_contamination: bool = True,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    on_retry=None,
) -> FactCheckResult:
    """Validate and (optionally) correct one document against its fact sheets."""
    result = FactCheckResult(slug=slug, filename=os.path.basename(doc.path), new_text=doc.render())
    unresolved: list[str] = []
    exams, primary = link_exams(
        doc, slug, registry, exam_binding=exam_binding, path=doc.path, unresolved=unresolved
    )
    if not exams:
        if unresolved:
            result.warnings.append(
                "frontmatter exam code(s) not found in the fact registry: "
                + ", ".join(dict.fromkeys(unresolved))
            )
        return result

    result.exams = [fact.code for fact in exams]
    body = doc.body
    # Protect fenced/inline code and URLs from scanning and rewriting, so a service
    # rename or a model ID inside a snippet is never treated as prose to fix.
    scan = mask(body)
    findings: list[FactFinding] = []
    conflicts = _rename_conflicts(exams)
    meta_text = f"{slug}\n{doc.frontmatter}"
    mentioned = [fact for fact in registry.facts if _mentions(fact, scan) or _mentions(fact, meta_text)]
    for fact in exams:
        if fact not in mentioned:
            mentioned.append(fact)
    # A bound document is judged on its own facts even when it cross-references another
    # exam; `mentioned` stays broad so a claim next to another exam is not misattributed.
    only_exam = (len(primary) == 1) if primary else (len(mentioned) == 1)
    single_primary = len(primary) == 1
    primary_exams = [fact for fact in exams if fact.code in primary]

    for fact in exams:
        is_primary = fact.code in primary
        allow_fix = fact.status == "active" and is_primary and only_exam
        _check_renames(fact, scan, findings, conflicts, mentioned, is_primary, single_primary)
        _check_services(fact, scan, findings)
        _check_values(fact, scan, findings, mentioned, only_exam, allow_fix)
        _check_dates(fact, scan, findings, mentioned, only_exam, allow_fix)
        if is_primary:
            _check_domains(fact, scan, findings, mentioned, only_exam)
            _check_grouped_domain_math(fact, scan, findings, mentioned, only_exam)
            if vendor_contamination:
                _check_vendor_contamination(fact, scan, findings, primary_exams)
        if fact.status == "retired":
            _flag_retired_status(fact, scan, findings)

    for finding in findings:
        if finding.applied and not fix:
            finding.applied = False
        if finding.applied:
            body = _replace(body, finding)
        result.findings.append(finding.to_dict())
        if not finding.applied:
            result.review_items.append(f"{finding.find}: {finding.reason}")
        if finding.source:
            result.sources.append(finding.source)

    if llm is not None and prompt:
        for fact in primary_exams:
            try:
                data = generate_fact_llm(fact, doc, slug, llm, prompt, max_attempts, on_retry)
            except Exception as err:  # noqa: BLE001 - keep the deterministic findings
                result.warnings.append(f"LLM fact check failed: {type(err).__name__}: {err}")
                continue
            body = _apply_llm_issues(body, data, result)

    result.new_text = _render(doc.frontmatter, body, doc.has_frontmatter)
    return result


def _render(frontmatter: str, body: str, has_frontmatter: bool) -> str:
    if has_frontmatter:
        return f"---\n{frontmatter}\n---\n{body}"
    return body
