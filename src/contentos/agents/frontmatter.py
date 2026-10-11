"""Frontmatter validator and corrector agent.

The schema lives in an external prompt file (``prompts/*.md``) so the same agent can
normalise a different project's frontmatter just by pointing ``--prompt`` at a new
file. The prompt is either:

* a Markdown file with the ExamOS layout (a ``FRONTMATTER SCHEMA`` table, an
  ``Authors`` table, a ``Categories`` list and a field-name alias table), or
* a Markdown file with optional YAML frontmatter that declares the same things
  explicitly (``fields``, ``authors``, ``categories``, ``exam_codes``, ``aliases``,
  ``category_synonyms``). The YAML takes precedence when present; the body is the
  system prompt used only by the optional LLM pass.

Everything is deterministic by default: this module repairs field order, names,
types, quoting, approved values, missing required fields (best-effort), duplicates,
study-plan fields on blogs, slug/filename mismatch, and can set ``order`` from a
slug -> integer order file. The body is never touched. The LLM is optional and only
fills missing values the deterministic rules cannot infer.
"""

from __future__ import annotations

import difflib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

import yaml

from contentos.agents.interlinker import DEFAULT_MAX_ATTEMPTS, _invoke_json

DEFAULT_PROMPT_PATH = os.path.join("prompts", "examos-blog-frontmatter.md")
DEFAULT_SEO_PROMPT = os.path.join("prompts", "examos_blog_seo_rewritre.md")

# The SEO rewriter must propose the three fields as JSON so code can validate and apply
# them; a bad response can never write an out-of-range title or break the frontmatter.
SEO_REWRITE_CONTRACT = """## OUTPUT (SEO REWRITE TASK)
Return ONLY a JSON object. No markdown, no prose, no code fence:
{
  "title": "the final title string",
  "seoTitle": "the final SEO title string",
  "seoDescription": "the final meta description string"
}
Include all three keys. Rewrite every field that is missing, shorter than its minimum,
or longer than its maximum; echo a field unchanged only when it already fits. No
exclamation marks, no em dashes, no filler phrases."""

SEO_BANNED_PHRASES = (
    "is coming",
    "the hidden risks nobody talks about",
    "everything you need to know",
    "a complete guide",
    "in today's world",
    "unlock",
)

DEFAULT_ORDER = 900
DEFAULT_AUTHOR = "ExamOS Expert"
STUDY_PLAN_FIELDS = ("provider", "estimatedHours", "passingScore", "targetAudience")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Categories that describe a broad audience rather than a subject, so they are poor
# signals when inferring an author from the post's primary category.
GENERIC_CATEGORIES = {
    "certifications",
    "certification",
    "cloud",
    "career",
    "strategy",
    "beginner",
    "preparation",
    "prep",
}

# Curated synonyms for the common non-approved tags. A prompt's YAML
# ``category_synonyms`` map extends/overrides these (a ``null`` value drops the tag).
DEFAULT_CATEGORY_SYNONYMS: dict[str, str | None] = {
    "pmp": "project-management",
    "management": "project-management",
    "agile": "project-management",
    "safe": "project-management",
    "scrum": "project-management",
    "certification": "certifications",
    "cert": "certifications",
    "certs": "certifications",
    "ml": "mlops",
    "machine-learning": "mlops",
    "genai": "generative-ai",
    "generativeai": "generative-ai",
    "gen-ai": "generative-ai",
    "googlecloud": "google-cloud",
    "examos": None,
}

# Fallback constants for the Markdown schema format when the Rules column does not
# spell them out. YAML schemas always take their ``const`` from the prompt.
DEFAULT_FIELD_CONSTS = {"type": "post", "imagePosition": "top"}

FIELD_HEADER_RE = re.compile(r"^\|\s*Order\s*\|\s*Field\s*\|\s*Required\s*\|", re.IGNORECASE)
FIELD_ROW_RE = re.compile(
    r"^\|\s*\d+\s*\|\s*`?([A-Za-z0-9_]+)`?\s*\|\s*(Yes|Optional|No)\s*\|\s*(.*?)\s*\|?\s*$"
)
HEADING_RE = re.compile(r"^#{1,6}\s+(.*?)\s*$")


class _FirstWinsLoader(yaml.SafeLoader):
    """A SafeLoader that keeps the first occurrence of a duplicate mapping key."""


def _first_wins_mapping(loader, node, deep=False):
    loader.flatten_mapping(node)
    result: dict = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            continue
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_FirstWinsLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _first_wins_mapping
)


@dataclass
class FieldSpec:
    key: str
    required: bool = False
    type: str = "text"
    const: str | None = None
    default: object = None
    only: str | None = None
    minimum: int | None = None
    maximum: int | None = None


@dataclass
class AuthorSpec:
    name: str
    specialty: str = ""
    categories: tuple[str, ...] = ()


@dataclass
class FrontmatterSchema:
    name: str
    system_prompt: str
    fields: list[FieldSpec]
    authors: list[AuthorSpec]
    categories: list[str]
    exam_codes: list[str]
    aliases: dict[str, str]
    category_synonyms: dict[str, str | None]
    exam_aliases: dict[str, str] = field(default_factory=dict)
    document_type: str = "blog"

    @property
    def field_map(self) -> dict[str, FieldSpec]:
        return {spec.key: spec for spec in self.fields}

    @property
    def author_names(self) -> list[str]:
        return [author.name for author in self.authors]


@dataclass
class FrontmatterResult:
    slug: str
    filename: str
    new_text: str
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    review_items: list[str] = field(default_factory=list)
    changes: list[dict] = field(default_factory=list)


# --- Schema loading -----------------------------------------------------------


def _infer_type(key: str) -> str:
    if key in {"order", "estimatedHours", "weight"}:
        return "int"
    if key in {"categories", "examCodes", "tags"}:
        return "list"
    if key == "date":
        return "date"
    return "text"


def _field_from_rules(key: str, required: bool, rules: str) -> FieldSpec:
    spec = FieldSpec(key=key, required=required, type=_infer_type(key))
    const_match = re.search(r"exactly\s*[`'\"]*([A-Za-z0-9_-]+)", rules)
    if const_match:
        spec.const = const_match.group(1)
    elif key in DEFAULT_FIELD_CONSTS:
        spec.const = DEFAULT_FIELD_CONSTS[key]
    range_match = re.search(r"(\d+)\s*-\s*(\d+)", rules)
    if range_match:
        spec.minimum = int(range_match.group(1))
        spec.maximum = int(range_match.group(2))
    if key == "order":
        spec.default = DEFAULT_ORDER
    if key in STUDY_PLAN_FIELDS:
        spec.only = "study_plan"
    return spec


def _parse_fields_from_markdown(text: str) -> list[FieldSpec]:
    lines = text.splitlines()
    fields: list[FieldSpec] = []
    for index, line in enumerate(lines):
        if not FIELD_HEADER_RE.match(line):
            continue
        for row in lines[index + 1 :]:
            match = FIELD_ROW_RE.match(row)
            if not match:
                if row.strip().startswith("|"):
                    continue
                if fields:
                    break
                continue
            key = match.group(1)
            required = match.group(2).lower() == "yes"
            fields.append(_field_from_rules(key, required, match.group(3)))
        break
    return fields


def _section_lines(text: str, names: tuple[str, ...]) -> list[str]:
    """Lines after the first heading whose title matches one of ``names``."""
    lines = text.splitlines()
    collected: list[str] = []
    active = False
    for line in lines:
        heading = HEADING_RE.match(line)
        if heading:
            if active:
                break
            active = heading.group(1).strip().lower() in names
            continue
        if active:
            collected.append(line)
    return collected


def _split_table_row(row: str) -> list[str]:
    cells = row.strip().strip("|").split("|")
    return [cell.strip() for cell in cells]


def _parse_authors_from_markdown(text: str) -> list[AuthorSpec]:
    lines = text.splitlines()
    authors: list[AuthorSpec] = []
    for index, line in enumerate(lines):
        cells = _split_table_row(line)
        lowered = [cell.lower() for cell in cells]
        if "author" in lowered and "specialty" in lowered:
            for row in lines[index + 1 :]:
                if not row.strip().startswith("|"):
                    break
                values = _split_table_row(row)
                if len(values) < 2 or set(values[0]) <= {"-"}:
                    continue
                name = values[0].strip()
                specialty = values[1].strip() if len(values) > 1 else ""
                categories: tuple[str, ...] = ()
                if len(values) > 2 and values[2]:
                    categories = tuple(
                        item.strip().lower() for item in values[2].split(",") if item.strip()
                    )
                if name:
                    authors.append(AuthorSpec(name=name, specialty=specialty, categories=categories))
            break
    return authors


def _parse_categories_from_markdown(text: str) -> list[str]:
    categories: list[str] = []
    for line in _section_lines(text, ("categories",)):
        match = re.match(r"^[-*]\s*`?([a-z0-9-]+)`?\s*$", line.strip())
        if match and match.group(1) not in categories:
            categories.append(match.group(1))
    return categories


def _parse_exam_codes_from_markdown(text: str) -> list[str]:
    section = "\n".join(_section_lines(text, ("exam codes",)))
    codes: list[str] = []
    for token in re.findall(r"[`'\"]([A-Za-z0-9][A-Za-z0-9_-]{1,})[`'\"]", section):
        if token == token.upper() and len(token) >= 2 and token not in codes:
            codes.append(token)
    return codes


def _parse_aliases_from_markdown(text: str) -> dict[str, str]:
    lines = text.splitlines()
    aliases: dict[str, str] = {}
    for index, line in enumerate(lines):
        lowered = [cell.lower() for cell in _split_table_row(line)]
        if "wrong" in lowered and "right" in lowered:
            for row in lines[index + 1 :]:
                if not row.strip().startswith("|"):
                    break
                values = _split_table_row(row)
                if len(values) < 2:
                    continue
                left = re.findall(r"`([A-Za-z0-9_]+)`", values[0])
                right = re.findall(r"`([A-Za-z0-9_]+)`", values[1])
                if left and right:
                    aliases[left[0]] = right[0]
            break
    return aliases


def _schema_from_markdown(name: str, body: str, document_type: str = "blog") -> FrontmatterSchema:
    fields = _parse_fields_from_markdown(body)
    if not fields:
        raise ValueError("Frontmatter prompt defines no schema table (Order/Field/Required).")
    categories = _parse_categories_from_markdown(body)
    if not categories:
        raise ValueError("Frontmatter prompt defines no approved categories.")
    return FrontmatterSchema(
        name=name,
        system_prompt=body.strip(),
        fields=fields,
        authors=_parse_authors_from_markdown(body),
        categories=categories,
        exam_codes=_parse_exam_codes_from_markdown(body),
        aliases=_parse_aliases_from_markdown(body),
        category_synonyms=dict(DEFAULT_CATEGORY_SYNONYMS),
        exam_aliases=dict(CURATED_NAME_ALIASES),
        document_type=document_type,
    )


def _schema_from_yaml(name: str, config: dict, body: str) -> FrontmatterSchema:
    raw_fields = config.get("fields") or []
    if not raw_fields:
        raise ValueError("Frontmatter prompt YAML defines no fields.")
    fields: list[FieldSpec] = []
    for entry in raw_fields:
        if isinstance(entry, str):
            entry = {"key": entry}
        spec = FieldSpec(
            key=str(entry["key"]),
            required=bool(entry.get("required", False)),
            type=str(entry.get("type", _infer_type(str(entry["key"])))),
            const=str(entry["const"]) if entry.get("const") is not None else None,
            default=entry.get("default"),
            only=entry.get("only"),
            minimum=int(entry["min"]) if entry.get("min") is not None else None,
            maximum=int(entry["max"]) if entry.get("max") is not None else None,
        )
        fields.append(spec)

    raw_authors = config.get("authors") or {}
    authors: list[AuthorSpec] = []
    if isinstance(raw_authors, dict):
        for author_name, details in raw_authors.items():
            details = details or {}
            authors.append(
                AuthorSpec(
                    name=str(author_name),
                    specialty=str(details.get("specialty", "")),
                    categories=tuple(str(item) for item in details.get("categories", [])),
                )
            )
    else:
        for entry in raw_authors:
            if isinstance(entry, str):
                entry = {"name": entry}
            authors.append(
                AuthorSpec(
                    name=str(entry["name"]),
                    specialty=str(entry.get("specialty", "")),
                    categories=tuple(str(item) for item in entry.get("categories", [])),
                )
            )

    synonyms = dict(DEFAULT_CATEGORY_SYNONYMS)
    for key, value in (config.get("category_synonyms") or {}).items():
        synonyms[str(key).strip().lower()] = None if value is None else str(value)

    exam_aliases = dict(CURATED_NAME_ALIASES)
    for key, value in (config.get("exam_aliases") or {}).items():
        exam_aliases[str(key).strip().lower()] = str(value)

    return FrontmatterSchema(
        name=name,
        system_prompt=body.strip(),
        fields=fields,
        authors=authors,
        categories=[str(item) for item in (config.get("categories") or [])],
        exam_codes=[str(item) for item in (config.get("exam_codes") or [])],
        aliases={str(k): str(v) for k, v in (config.get("aliases") or {}).items()},
        category_synonyms=synonyms,
        exam_aliases=exam_aliases,
        document_type=str(config.get("document_type", "blog")),
    )


def load_frontmatter_schema(path: str = DEFAULT_PROMPT_PATH) -> FrontmatterSchema:
    """Load a frontmatter prompt: optional YAML config + Markdown schema tables."""
    from contentos.core.loaders import load_document

    doc = load_document(path)
    name = os.path.splitext(os.path.basename(path))[0]
    if doc.has_frontmatter:
        try:
            config = yaml.safe_load(doc.frontmatter) or {}
        except yaml.YAMLError as err:
            raise ValueError(f"Invalid YAML frontmatter in {path}: {err}") from err
        if isinstance(config, dict) and config.get("fields"):
            return _schema_from_yaml(
                str(config.get("name") or name), config, doc.body
            )
        return _schema_from_markdown(
            str(config.get("name") or name),
            doc.body,
            str(config.get("document_type", "blog")),
        )
    return _schema_from_markdown(name, doc.body)


@dataclass(frozen=True)
class SeoRewriteSpec:
    """The external SEO rewriter prompt (system prompt + name)."""

    name: str
    system_prompt: str


def load_seo_spec(path: str = DEFAULT_SEO_PROMPT) -> SeoRewriteSpec:
    """Load the SEO rewrite prompt body (Markdown; YAML frontmatter stripped if present)."""
    from contentos.core.loaders import load_document

    doc = load_document(path)
    name = os.path.splitext(os.path.basename(path))[0]
    if doc.has_frontmatter:
        try:
            config = yaml.safe_load(doc.frontmatter) or {}
            name = str(config.get("name") or name)
        except yaml.YAMLError:
            pass
    return SeoRewriteSpec(name=name, system_prompt=doc.body.strip())


def build_seo_rewrite_prompt(
    schema: FrontmatterSchema, doc, slug: str, values: dict, feedback: str = ""
) -> str:
    def _current(key: str, spec: FieldSpec) -> str:
        value = values.get(key)
        if value is None:
            return "(missing)"
        text = str(value).strip()
        return f"{text} ({len(text)} chars)"

    parts = [
        f"SOURCE SLUG: {slug}",
        f"FILENAME: {os.path.basename(doc.path)}",
        "",
        "CURRENT METADATA:",
    ]
    for key in ("title", "seoTitle", "seoDescription"):
        spec = schema.field_map.get(key)
        if spec is not None:
            parts.append(f"- {key}: {_current(key, spec)}")
    parts.append("")
    parts.append("FULL BODY:")
    parts.append(doc.body)
    parts.append("")
    parts.append(
        "Rewrite every field that is missing or outside its target range. "
        "Return only the JSON object."
    )
    if feedback:
        parts.append("")
        parts.append("YOUR PREVIOUS ATTEMPT FAILED VALIDATION:")
        parts.append(feedback)
        parts.append("Return corrected values that fix each problem exactly.")
    return "\n".join(parts)


def generate_seo_rewrite(
    schema: FrontmatterSchema,
    doc,
    slug: str,
    values: dict,
    llm,
    spec: SeoRewriteSpec,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    on_retry=None,
    feedback: str = "",
) -> dict:
    from langchain_core.messages import HumanMessage, SystemMessage

    system = f"{spec.system_prompt}\n\n{SEO_REWRITE_CONTRACT}"
    messages = [
        SystemMessage(content=system),
        HumanMessage(content=build_seo_rewrite_prompt(schema, doc, slug, values, feedback)),
    ]
    data = _invoke_json(llm, messages, max_attempts=max_attempts, on_retry=on_retry)
    return data if isinstance(data, dict) else {}


# --- Exam registry and detection ----------------------------------------------

DEFAULT_EXAM_DB = "exam_database.txt"

# Vendor prefixes and generic role suffixes are stripped when deriving a name
# alias ("AWS Certified Solutions Architect - Associate" -> "solutions architect"
# style aliases stay conservative: only a leading vendor prefix and trailing role
# words are removed, so the alias keeps at least two significant words).
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
_ROLE_SUFFIXES = {
    "associate",
    "professional",
    "expert",
    "practitioner",
    "fundamentals",
    "foundations",
    "engineer",
    "developer",
    "administrator",
    "architect",
    "specialty",
    "leader",
}
_VERSION_RE = re.compile(r"v?\d+([.]\d+)*$")

# Curated aliases for exams whose common name does not reduce cleanly from the
# database name (matched only when the target code is in the schema's valid list).
# A prompt's YAML ``exam_aliases`` map extends/overrides these.
CURATED_NAME_ALIASES: dict[str, str] = {
    "professional cloud architect": "GCP-PCA",
    "associate cloud engineer": "GCP-ACE",
    "cloud digital leader": "GCP-CDL",
    "professional cloud network engineer": "GCP-PCNE",
    "professional data engineer": "GCP-PDE",
    "generative ai leader": "GCP-GAI",
}


@dataclass(frozen=True)
class ExamEntry:
    code: str
    name: str


def load_exam_registry(path: str = DEFAULT_EXAM_DB) -> list[ExamEntry]:
    """Load ``[{code, name}, ...]`` from the exam database JSON. Missing -> empty."""
    if not path or not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return []
    entries: list[ExamEntry] = []
    for item in data if isinstance(data, list) else []:
        if isinstance(item, dict) and item.get("code"):
            entries.append(ExamEntry(code=str(item["code"]), name=str(item.get("name") or "")))
    return entries


def _match_text(value: object) -> str:
    """Normalise text for matching: lowercase, ``+``/``&`` spelled out."""
    text = str(value).lower().replace("+", " plus ").replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _code_regex(code: str) -> re.Pattern[str]:
    """A tolerant code pattern: ``-``/``_``/space separators are interchangeable."""
    parts = [part for part in re.split(r"[-_ ]+", code) if part]
    core = r"[-_ ]?".join(re.escape(part) for part in parts)
    return re.compile(rf"(?<![a-z0-9]){core}(?![a-z0-9])", re.IGNORECASE)


def _name_aliases(name: str, broad: bool = False) -> set[str]:
    """Aliases for an exam name, requiring two significant words (precision first).

    ``_core_alias`` strips a leading vendor prefix and the words "certified"/"certification"
    so the common name matches ("AWS Certified Cloud Practitioner" -> "cloud practitioner").
    Names with a ``+`` (Security+, CySA+, ...) skip that step so "Security + Data" is never
    read as the Security+ certification. ``broad`` adds the vendor-only and role-stripped
    forms and is used only in metadata (slug/title/description).
    """
    normalized = _match_text(name)
    if not normalized:
        return set()
    aliases = {normalized}
    tokens = [token for token in normalized.split() if not _VERSION_RE.fullmatch(token)]
    aliases.add(" ".join(tokens))
    core = _core_alias(tokens) if "+" not in str(name) else tokens
    aliases.add(" ".join(core))
    if broad:
        stripped = list(tokens)
        while stripped and stripped[0] in _VENDOR_PREFIXES:
            stripped.pop(0)
        aliases.add(" ".join(stripped))
        trimmed = list(core)
        if trimmed and trimmed[-1] in _ROLE_SUFFIXES:
            trimmed.pop()
        aliases.add(" ".join(trimmed))
    return {alias for alias in aliases if len(alias.split()) >= 2 and len(alias) >= 8}


def _core_alias(tokens: list[str]) -> list[str]:
    core = list(tokens)
    while core and core[0] in _VENDOR_PREFIXES:
        core.pop(0)
    return [token for token in core if token not in {"certified", "certification"}]


# Vendor families used to keep vendor-stripped aliases from colliding across vendors
# (an AWS "developer associate" must not match an Azure "Developer Associate").
_VENDOR_FAMILIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("azure", ("azure", "microsoft")),
    ("aws", ("aws", "amazon")),
    ("gcp", ("google",)),
    ("comptia", ("comptia",)),
    ("isaca", ("isaca",)),
    ("pmi", ("pmi",)),
    ("cisco", ("cisco",)),
    ("hashicorp", ("hashicorp", "terraform")),
    ("cncf", ("kubernetes", "cncf")),
    ("anthropic", ("claude", "anthropic")),
    ("agile", ("scrum", "safe")),
    ("github", ("github",)),
)


def _vendor_families(text: str) -> set[str]:
    """Vendor families named in ``text`` (already or not yet normalized)."""
    low = _match_text(text)
    found: set[str] = set()
    for family, tokens in _VENDOR_FAMILIES:
        if any(
            re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", low)
            for token in tokens
        ):
            found.add(family)
    return found


def _vendor_family(name: str) -> str | None:
    """The single vendor family for an exam name, or ``None`` when ambiguous/unknown."""
    families = _vendor_families(name)
    return next(iter(families)) if len(families) == 1 else None


def _allowed_exam_codes(
    schema: FrontmatterSchema, registry: list[ExamEntry] | None
) -> list[str]:
    """The valid code list: the prompt's examples plus every database code (union)."""
    codes = list(schema.exam_codes)
    for entry in registry or []:
        if entry.code not in codes:
            codes.append(entry.code)
    return codes


def _entry_matches(
    text: str,
    code: str,
    entry: ExamEntry | None,
    broad: bool,
    *,
    vendor_context: str | None = None,
) -> bool:
    """Whether ``text`` names ``code``.

    A vendor-stripped alias (``developer associate``, ``cloud practitioner``) is
    accepted only when ``vendor_context`` names the exam's vendor, so an AWS alias
    cannot match another vendor's similarly named role. ``vendor_context=None``
    disables that guard (used for metadata, where the alias is a strong signal).
    """
    if _code_regex(code).search(text):
        return True
    if entry is None:
        return False
    full = _match_text(entry.name)
    family = _vendor_family(entry.name)
    for alias in _name_aliases(entry.name, broad=broad):
        for match in re.finditer(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", text):
            if vendor_context is not None and alias != full and family is not None:
                window = text[max(0, match.start() - 160) : match.end() + 160]
                if family not in _vendor_families(window) and full not in text:
                    continue
            return True
    return False


def _dominant_vendor(meta: str, meta_hits: list[str], by_code: dict[str, ExamEntry]) -> str | None:
    """The single vendor family of the metadata, or ``None`` when mixed/unknown."""
    families = _vendor_families(meta)
    for code in meta_hits:
        entry = by_code.get(code.upper())
        if entry is not None:
            family = _vendor_family(entry.name)
            if family is not None:
                families.add(family)
    return next(iter(families)) if len(families) == 1 else None


def _incidental_code_mention(code: str, body: str) -> bool:
    """True when the exam code appears once in prose (not in a heading)."""
    matches = list(_code_regex(code).finditer(body))
    if len(matches) != 1:
        return False
    match = matches[0]
    line_start = body.rfind("\n", 0, match.start()) + 1
    line_end = body.find("\n", match.end())
    if line_end == -1:
        line_end = len(body)
    return not body[line_start:line_end].lstrip().startswith("#")


def detect_exam_codes(
    schema: FrontmatterSchema,
    doc,
    slug: str,
    values: dict,
    registry: list[ExamEntry] | None = None,
) -> list[str]:
    """Detect every valid exam the content references (metadata first, then body).

    Explicit code mentions and confidence name aliases are matched against the
    schema's valid exam list (falling back to the registry's codes). The result is
    ordered by relevance: slug/title/meta matches first, then body mentions. Broad
    name aliases are used in metadata only, conservative aliases in the body.

    Two precision guards keep a passing reference from tagging the post with an
    unrelated exam: a vendor-stripped alias in the body must appear near its vendor,
    and a body code from a different vendor than the document's dominant one is
    ignored when it is only a single prose mention.
    """
    if "examCodes" not in schema.field_map:
        return []
    registry = registry or []
    valid = _allowed_exam_codes(schema, registry)
    if not valid:
        return []
    by_code = {entry.code.upper(): entry for entry in registry}
    curated: dict[str, list[str]] = {}
    for alias, target in schema.exam_aliases.items():
        curated.setdefault(target.upper(), []).append(alias)
    meta = _match_text(
        " ".join(
            [
                slug,
                str(values.get("title") or ""),
                str(values.get("seoTitle") or ""),
                str(values.get("seoDescription") or ""),
            ]
        )
    )
    body = _match_text(doc.body)
    meta_hits: list[str] = []
    body_hits: list[str] = []
    for code in valid:
        key = code.upper()
        entry = by_code.get(key)
        aliases = curated.get(key, [])
        if _entry_matches(meta, code, entry, broad=True) or _curated_matches(meta, aliases):
            meta_hits.append(code)
        elif _entry_matches(
            body, code, entry, broad=False, vendor_context=body
        ) or _curated_matches(body, aliases):
            body_hits.append(code)

    dominant = _dominant_vendor(meta, meta_hits, by_code)
    if dominant is not None:
        body_hits = [
            code
            for code in body_hits
            if not _cross_vendor_incidental(code, doc.body, dominant, by_code)
        ]
    return meta_hits + body_hits


def _cross_vendor_incidental(
    code: str, raw_body: str, dominant: str, by_code: dict[str, ExamEntry]
) -> bool:
    entry = by_code.get(code.upper())
    if entry is None:
        return False
    family = _vendor_family(entry.name)
    if family is None or family == dominant:
        return False
    return _incidental_code_mention(code, raw_body)



def _curated_matches(text: str, aliases: list[str]) -> bool:
    return any(
        re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", text) for alias in aliases
    )


# --- Frontmatter parsing ------------------------------------------------------


def _safe_load_first(text: str):
    return yaml.load(text, Loader=_FirstWinsLoader)


def parse_frontmatter(frontmatter: str) -> tuple[dict, list[str], list[str]]:
    """Return ``(values, order, duplicates)`` with the first duplicate kept."""
    keys = re.findall(r"(?m)^([A-Za-z0-9_.\-]+)\s*:", frontmatter)
    seen: set[str] = set()
    duplicates: list[str] = []
    for key in keys:
        if key in seen:
            duplicates.append(key)
        seen.add(key)
    if not frontmatter.strip():
        return {}, [], duplicates
    try:
        data = _safe_load_first(frontmatter)
    except yaml.YAMLError:
        data = None
    if isinstance(data, dict):
        return data, [str(key) for key in data], duplicates
    values: dict = {}
    order: list[str] = []
    for line in frontmatter.splitlines():
        match = re.match(r"^([A-Za-z0-9_.\-]+)\s*:(.*)$", line)
        if not match:
            continue
        key = match.group(1)
        if key in values:
            continue
        raw = match.group(2).strip()
        try:
            values[key] = yaml.safe_load(raw)
        except yaml.YAMLError:
            values[key] = raw.strip("\"'")
        order.append(key)
    return values, order, duplicates


def _quote_scalar(value: object) -> str:
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    text = text.replace("\r", " ").replace("\n", " ")
    return f'"{text}"'


def _is_scalar_value(value: object) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _dump_yaml(key: str, value: object) -> str:
    """Render a complex value (dict / list of dicts) as real YAML under ``key``.

    ``yaml.safe_dump`` is used so nested fields survive a round-trip instead of the
    old ad-hoc scalar formatting, which corrupted map/list structures. Every dumped
    line is indented two spaces below the field.
    """
    dumped = yaml.safe_dump(
        value, default_flow_style=False, sort_keys=False, allow_unicode=True
    ).rstrip("\n")
    if not dumped:
        return f"{key}:"
    lines = dumped.splitlines()
    body = "\n".join(f"  {line}" for line in lines)
    return f"{key}:\n{body}"


def _emit(spec: FieldSpec, value: object) -> str:
    if spec.type == "int":
        try:
            return f"{spec.key}: {int(value)}"
        except (TypeError, ValueError):
            return f"{spec.key}: {DEFAULT_ORDER}"
    if spec.type == "list":
        items = value if isinstance(value, list) else _as_list(value)
        if any(not _is_scalar_value(item) for item in items):
            return _dump_yaml(spec.key, items)
        rendered = ", ".join(_quote_scalar(item) for item in items)
        return f"{spec.key}: [{rendered}]"
    if not _is_scalar_value(value):
        return _dump_yaml(spec.key, value)
    return f"{spec.key}: {_quote_scalar(value)}"


def _emit_unknown(key: str, value: object) -> str:
    if isinstance(value, bool):
        return f"{key}: {'true' if value else 'false'}"
    if isinstance(value, int):
        return f"{key}: {value}"
    if isinstance(value, float):
        return f"{key}: {value}"
    if isinstance(value, str):
        return f"{key}: {_quote_scalar(value)}"
    if isinstance(value, list) and all(_is_scalar_value(item) for item in value):
        rendered = ", ".join(_quote_scalar(item) for item in value)
        return f"{key}: [{rendered}]"
    return _dump_yaml(key, value)


def _extract_raw_field(frontmatter: str, key: str) -> str | None:
    """Return the raw source block for ``key`` (its line plus indented continuations).

    Used to re-emit ``nested_preserve`` fields verbatim, so deeply nested roadmap YAML
    is never re-serialized and cannot be lost.
    """
    lines = frontmatter.splitlines()
    start = None
    pattern = re.compile(rf"^{re.escape(key)}\s*:")
    for index, line in enumerate(lines):
        if pattern.match(line):
            start = index
            break
    if start is None:
        return None
    block = [lines[start]]
    for line in lines[start + 1 :]:
        if not line.strip():
            block.append(line)
            continue
        if line[:1] not in (" ", "\t", "-"):
            break
        block.append(line)
    return "\n".join(block).rstrip("\n")



# --- Value coercion -----------------------------------------------------------


def _as_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return [] if value is None else [str(value).strip()]


def _as_int(value: object) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _as_date(value: object, today: str) -> str:
    if isinstance(value, (date,)):
        return value.isoformat()
    text = str(value).strip().strip("\"'")
    if DATE_RE.match(text):
        return text
    return today


def _records_changed(before, after) -> bool:
    return before != after


# --- Derived values -----------------------------------------------------------


def _first_h1(body: str) -> str:
    match = re.search(r"(?m)^#\s+(.+?)\s*$", body)
    if not match:
        return ""
    text = match.group(1)
    text = re.sub(r"[*_`]", "", text)
    return text.strip()


def _humanize(slug: str) -> str:
    words = [word for word in re.split(r"[-_]", slug) if word]
    if not words:
        return slug
    return " ".join(word.capitalize() for word in words)


def _first_paragraph(body: str) -> str:
    lines = body.splitlines()
    paragraph: list[str] = []
    started = False
    for line in lines:
        stripped = line.strip()
        if not started:
            if not stripped or stripped.startswith(("#", "<")):
                continue
            started = True
        if started and not stripped:
            break
        if started:
            paragraph.append(stripped)
    text = " ".join(paragraph)
    text = re.sub(r"[*_`]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _trim_to(text: str, maximum: int) -> str:
    if len(text) <= maximum:
        return text
    cut = text[: maximum + 1]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,.;:") + "."


def _seo_out_of_range(value: object, spec: FieldSpec) -> bool:
    if value is None:
        return True
    text = str(value).strip()
    if not text:
        return True
    if spec.minimum is not None and len(text) < spec.minimum:
        return True
    return bool(spec.maximum is not None and len(text) > spec.maximum)


def _needs_seo_rewrite(value: object, spec: FieldSpec) -> bool:
    """True when an SEO field is missing, out of range, or carries a banned tell."""
    if _seo_out_of_range(value, spec):
        return True
    text = str(value)
    lowered = text.lower()
    if "!" in text or "\u2014" in text or "\u2013" in text:
        return True
    return any(phrase in lowered for phrase in SEO_BANNED_PHRASES)


def _seo_candidate_problem(candidate: object, spec: FieldSpec, title: object = None) -> str:
    """Return why a rewrite candidate is unusable, or ``""`` when it passes."""
    if not isinstance(candidate, str) or not candidate.strip():
        return "was missing or empty"
    text = candidate.strip()
    if "!" in text or "\u2014" in text or "\u2013" in text:
        return "contained an exclamation mark or em dash"
    if spec.minimum is not None and len(text) < spec.minimum:
        return f"was {len(text)} characters, below the {spec.minimum}-character minimum"
    if spec.maximum is not None and len(text) > spec.maximum:
        return f"was {len(text)} characters, above the {spec.maximum}-character maximum"
    if any(phrase in text.lower() for phrase in SEO_BANNED_PHRASES):
        return "contained a banned filler phrase"
    if spec.key == "seoTitle" and str(title or "").strip() == text:
        return "duplicated the title verbatim"
    return ""


def _check_length(key: str, value: object, spec: FieldSpec, result: FrontmatterResult) -> None:
    """Flag (never trim) an SEO field that is still outside its target range."""
    if spec.minimum is None and spec.maximum is None:
        return
    text = str(value)
    if spec.minimum is not None and len(text) < spec.minimum:
        maximum = spec.maximum if spec.maximum is not None else "-"
        result.review_items.append(
            f"{key} is {len(text)} characters (target {spec.minimum}-{maximum}); needs expansion"
        )
    elif spec.maximum is not None and len(text) > spec.maximum:
        minimum = spec.minimum if spec.minimum is not None else "-"
        result.review_items.append(
            f"{key} is {len(text)} characters (target {minimum}-{spec.maximum}); needs shortening"
        )


def _infer_categories(schema: FrontmatterSchema, slug: str, values: dict, body: str) -> list[str]:
    approved = set(schema.categories)
    haystack = " ".join(
        [
            slug.replace("-", " "),
            str(values.get("title") or ""),
            str(values.get("seoTitle") or ""),
            " ".join(_as_list(values.get("examCodes"))),
            body[:2000],
        ]
    ).lower()
    found: list[str] = []
    for code in _as_list(values.get("examCodes")):
        for category in _exam_code_categories(code):
            if category in approved and category not in found:
                found.append(category)
    for category in schema.categories:
        if category in found:
            continue
        if re.search(rf"\b{re.escape(category)}\b", haystack):
            found.append(category)
    return found[:3]


_EXAM_CODE_CATEGORIES = (
    (r"^AZ-", ("azure", "cloud")),
    (r"^AI-", ("ai", "azure")),
    (r"^DP-", ("data", "ai")),
    (r"^SC-", ("security", "azure")),
    (r"^(SAA|SAP|DOP|CLF|AIF|MLA)-", ("aws", "cloud")),
    (r"^(GCP|PCA)-", ("gcp", "google-cloud")),
    (r"^(CKA|CKS)", ("kubernetes", "devops")),
    (r"^(CISSP|CISM|CCSP|CISA|CRISC|SY0)", ("security", "certifications")),
    (r"^PMP", ("project-management", "certifications")),
)


def _exam_code_categories(code: str) -> tuple[str, ...]:
    upper = code.upper()
    for pattern, categories in _EXAM_CODE_CATEGORIES:
        if re.match(pattern, upper):
            return categories
    return ()


def _infer_author(schema: FrontmatterSchema, categories: list[str]) -> str:
    for category in categories:
        if category in GENERIC_CATEGORIES:
            continue
        for author in schema.authors:
            if category in author.categories:
                return author.name
    if DEFAULT_AUTHOR in schema.author_names:
        return DEFAULT_AUTHOR
    return schema.author_names[0] if schema.author_names else DEFAULT_AUTHOR


# --- Normalisation ------------------------------------------------------------


def _normalize_categories(categories: list[str], schema: FrontmatterSchema) -> tuple[list[str], list[str]]:
    approved = set(schema.categories)
    kept: list[str] = []
    review: list[str] = []
    for raw in categories:
        value = str(raw).strip().lower()
        if value in approved:
            kept.append(value)
            continue
        if value in schema.category_synonyms:
            mapped = schema.category_synonyms[value]
            if mapped is None:
                review.append(f"dropped unapproved category '{raw}'")
            elif mapped in approved:
                if mapped not in kept:
                    kept.append(mapped)
                review.append(f"category '{raw}' mapped to '{mapped}'")
            else:
                review.append(f"dropped unapproved category '{raw}'")
            continue
        close = difflib.get_close_matches(value, list(approved), n=1, cutoff=0.8)
        if close:
            if close[0] not in kept:
                kept.append(close[0])
            review.append(f"category '{raw}' replaced with closest match '{close[0]}'")
        else:
            review.append(f"dropped unapproved category '{raw}'")
    return sorted(dict.fromkeys(kept)), review


def _normalize_author(value: str, schema: FrontmatterSchema, categories: list[str]) -> tuple[str, list[str]]:
    if value in schema.author_names:
        return value, []
    close = difflib.get_close_matches(value, schema.author_names, n=1, cutoff=0.6)
    if close:
        return close[0], [f"author '{value}' is not approved; replaced with closest match '{close[0]}'"]
    inferred = _infer_author(schema, categories)
    return inferred, [f"author '{value}' is not approved; inferred '{inferred}' from the post's category"]


def _normalize_exam_codes(codes: list[str], allowed_codes: list[str]) -> tuple[list[str], list[str]]:
    if not allowed_codes:
        return codes, []
    allowed = {code.upper(): code for code in allowed_codes}
    kept: list[str] = []
    review: list[str] = []
    for raw in codes:
        canonical = allowed.get(str(raw).strip().upper())
        if canonical:
            if canonical not in kept:
                kept.append(canonical)
        else:
            review.append(f"removed invalid exam code '{raw}'")
    return kept, review


# --- LLM inference (optional) -------------------------------------------------


INFER_CONTRACT = """## OUTPUT (FRONTMATTER TASK)
Ignore any earlier instructions about returning two files or a changelog. You are
only filling the missing fields listed below. Return ONLY a JSON object, no prose
and no code fence, using the keys that were requested:
{
  "slug": "...",
  "title": "...",
  "seoTitle": "...",
  "seoDescription": "...",
  "author": "one approved author name",
  "categories": ["approved", "categories"],
  "examCodes": ["VALID-CODE"],
  "date": "YYYY-MM-DD"
}
Never invent an author, category or exam code that is not in the approved lists.
Return an empty string / empty list when a field cannot be determined."""


def _build_infer_prompt(
    schema: FrontmatterSchema, doc, slug: str, missing: list[str], values: dict
) -> str:
    approved_authors = ", ".join(schema.author_names) or "(none)"
    approved_categories = ", ".join(schema.categories) or "(none)"
    approved_codes = ", ".join(schema.exam_codes) or "(none)"
    return (
        f"SOURCE SLUG: {slug}\n"
        f"FILENAME: {os.path.basename(doc.path)}\n"
        f"MISSING FIELDS: {', '.join(missing) or '(none)'}\n\n"
        f"APPROVED AUTHORS: {approved_authors}\n"
        f"APPROVED CATEGORIES: {approved_categories}\n"
        f"APPROVED EXAM CODES: {approved_codes}\n\n"
        f"CURRENT FRONTMATTER:\n{doc.frontmatter or '(none)'}\n\n"
        f"BODY:\n{doc.body}\n"
    )


def infer_missing_values(
    schema: FrontmatterSchema,
    doc,
    slug: str,
    missing: list[str],
    values: dict,
    llm,
    max_attempts: int,
    on_retry=None,
) -> dict:
    from langchain_core.messages import HumanMessage, SystemMessage

    if not missing:
        return {}
    system = f"{schema.system_prompt}\n\n{INFER_CONTRACT}"
    messages = [
        SystemMessage(content=system),
        HumanMessage(content=_build_infer_prompt(schema, doc, slug, missing, values)),
    ]
    data = _invoke_json(llm, messages, max_attempts=max_attempts, on_retry=on_retry)
    return data if isinstance(data, dict) else {}


# --- Correction ---------------------------------------------------------------


def _render(frontmatter: str, body: str) -> str:
    return f"---\n{frontmatter}\n---\n{body}"


def _apply_order_only(frontmatter: str, order_value: int) -> str:
    """Replace just the ``order`` line, preserving every other byte."""
    pattern = re.compile(r"(?m)^(order\s*:\s*)([^\n]*)$")
    if pattern.search(frontmatter):
        return pattern.sub(lambda match: f"{match.group(1)}{order_value}", frontmatter, count=1)
    if not frontmatter:
        return f"order: {order_value}"
    separator = "" if frontmatter.endswith("\n") else "\n"
    return f"{frontmatter}{separator}order: {order_value}"


def correct_frontmatter(
    doc,
    schema: FrontmatterSchema,
    *,
    order_map: dict[str, int] | None = None,
    only_order: bool = False,
    llm=None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    on_retry=None,
    today: str | None = None,
    exam_registry: list[ExamEntry] | None = None,
    detect_exams: bool = True,
    seo_llm=None,
    seo_spec: SeoRewriteSpec | None = None,
    nested_preserve: tuple[str, ...] = (),
) -> FrontmatterResult:
    """Validate and correct one document's frontmatter. The body is untouched."""
    filename = os.path.basename(doc.path)
    stem = os.path.splitext(filename)[0]
    slug = stem
    today = today or datetime.now(UTC).date().isoformat()
    result = FrontmatterResult(slug=slug, filename=filename, new_text=doc.render())

    if only_order:
        if not order_map or slug not in order_map:
            return result
        order_value = _as_int(order_map[slug])
        if order_value is None:
            result.warnings.append(f"order file value for '{slug}' is not an integer")
            return result
        frontmatter = _apply_order_only(doc.frontmatter, order_value)
        result.notes.append(f"order set to {order_value}")
        result.changes.append(
            {
                "field": "order",
                "before": _value_before(doc.frontmatter, "order"),
                "after": str(order_value),
                "reason": "order file",
            }
        )
        result.new_text = _render(frontmatter, doc.body)
        return result

    values, _, duplicates = parse_frontmatter(doc.frontmatter)
    for duplicate in duplicates:
        result.notes.append(f"removed duplicate field '{duplicate}'")

    for old, new in schema.aliases.items():
        if old in values and old not in schema.field_map and new in schema.field_map:
            values[new] = values.pop(old)
            result.notes.append(f"renamed field '{old}' to '{new}'")

    declared_type = str(values.get("type", "")).strip().lower()
    is_study_plan = (
        schema.document_type == "study_plan"
        or declared_type in {"study-plan", "study_plan", "studyplan"}
        or (
            schema.document_type == "blog"
            and "type" not in values
            and any(field_name in values for field_name in STUDY_PLAN_FIELDS)
        )
    )
    kind = "study_plan" if is_study_plan else "blog"

    if kind == "study_plan" and "examCodes" in values and "examCode" in schema.field_map:
        raw_codes = values.pop("examCodes")
        if isinstance(raw_codes, list):
            raw_codes = next((str(code).strip() for code in raw_codes if str(code).strip()), "")
        values["examCode"] = raw_codes
        result.notes.append("renamed 'examCodes' to 'examCode' for a study plan")

    # Detect every valid exam the content references and fill examCodes so related
    # content can be tied together. Existing valid codes are preserved and merged.
    if detect_exams and kind == "blog" and "examCodes" in schema.field_map:
        detected = detect_exam_codes(schema, doc, slug, values, exam_registry)
        if detected:
            existing = _as_list(values.get("examCodes"))
            merged = list(dict.fromkeys([*existing, *detected]))
            if merged != existing:
                values["examCodes"] = merged
            newly_added = [code for code in detected if code not in existing]
            if newly_added:
                result.notes.append(f"detected examCodes: {', '.join(newly_added)}")
                result.changes.append(
                    {
                        "field": "examCodes",
                        "before": _format_value(existing) if existing else "(missing)",
                        "after": _format_value(merged),
                        "reason": "exam codes detected in the content",
                    }
                )

    # SEO rewrite (LLM): rewrite title/seoTitle/seoDescription that are missing or
    # outside their target range. Only validated candidates are applied, so a bad
    # response can never write an out-of-range title or a filler phrase.
    if seo_llm is not None and seo_spec is not None and kind == "blog":
        seo_fields = {
            key: schema.field_map[key]
            for key in ("title", "seoTitle", "seoDescription")
            if key in schema.field_map
        }
        pending = [key for key, field_spec in seo_fields.items() if _needs_seo_rewrite(values.get(key), field_spec)]
        feedback = ""
        for attempt in range(2):
            if not pending:
                break
            try:
                proposal = generate_seo_rewrite(
                    schema, doc, slug, values, seo_llm, seo_spec, max_attempts, on_retry, feedback
                )
            except Exception as err:  # noqa: BLE001 - keep the rest of the corrections
                result.warnings.append(f"SEO rewrite failed: {type(err).__name__}: {err}")
                break
            still_pending: list[str] = []
            feedback_lines: list[str] = []
            for key in pending:
                field_spec = seo_fields[key]
                candidate = proposal.get(key)
                before = values.get(key)
                candidate_text = str(candidate).strip() if isinstance(candidate, str) else ""
                problem = _seo_candidate_problem(candidate, field_spec, values.get("title"))
                if problem:
                    still_pending.append(key)
                    feedback_lines.append(f"- {key}: {problem}")
                    continue
                values[key] = candidate_text
                result.notes.append(
                    f"rewrote {key} ({len(str(before or ''))} -> {len(candidate_text)} chars)"
                )
                result.changes.append(
                    {
                        "field": key,
                        "before": _format_value(before) if before is not None else "(missing)",
                        "after": candidate_text,
                        "reason": "LLM SEO rewrite (out of range)",
                    }
                )
            pending = still_pending
            feedback = "\n".join(feedback_lines)
        for key in pending:
            result.warnings.append(
                f"{key} rewrite rejected (missing, out of range, or banned phrasing)"
            )

    missing = [
        spec.key
        for spec in schema.fields
        if spec.required
        and spec.key not in values
        and spec.only != "study_plan"
        and spec.const is None
        and spec.default is None
    ]
    if llm is not None and missing:
        try:
            proposed = infer_missing_values(schema, doc, slug, missing, values, llm, max_attempts, on_retry)
        except Exception as err:  # noqa: BLE001 - fall back to deterministic defaults
            proposed = {}
            result.warnings.append(f"LLM inference failed: {type(err).__name__}: {err}")
        for key in missing:
            candidate = proposed.get(key)
            if candidate not in (None, "", []):
                values[key] = candidate
                result.notes.append(f"filled '{key}' with the LLM proposal")

    normalized_categories: list[str] | None = None
    if "categories" in values:
        normalized_categories, _ = _normalize_categories(_as_list(values["categories"]), schema)
    allowed_exam_codes = _allowed_exam_codes(schema, exam_registry)

    emitted: list[str] = []
    emitted_keys: list[str] = []
    for spec in schema.fields:
        key = spec.key
        present = key in values
        if nested_preserve and key in nested_preserve and present:
            raw = _extract_raw_field(doc.frontmatter, key)
            if raw is not None:
                emitted.append(raw)
                emitted_keys.append(key)
                continue
        if (
            spec.only == "study_plan"
            and schema.document_type == "blog"
            and kind != "study_plan"
        ):
            if present:
                result.notes.append(f"removed study-plan field '{key}'")
                result.changes.append(
                    {
                        "field": key,
                        "before": _format_value(values[key]),
                        "after": "(removed)",
                        "reason": "field is not valid on a blog",
                    }
                )
            continue

        value, record_change = _resolve_value(spec, values, schema, slug, doc.body, kind, today, result)
        if value is None:
            continue

        reason_override: str | None = None
        if spec.const is not None:
            if present and str(values[key]).strip() != spec.const:
                record_change = True
            value = spec.const

        if key == "slug":
            if str(value) != slug:
                record_change = True
                value = slug
        elif key == "order":
            order_value = _as_int(value)
            value = order_value if order_value is not None else DEFAULT_ORDER
            if order_map and slug in order_map:
                mapped = _as_int(order_map[slug])
                if mapped is not None and mapped != value:
                    value = mapped
                    record_change = True
                    reason_override = "order file"
            if not present:
                record_change = True
        elif key == "categories":
            original = _as_list(value)
            value = normalized_categories if normalized_categories is not None else original
            _, review = _normalize_categories(original, schema)
            result.review_items.extend(review)
            if review or value != original:
                record_change = True
        elif key == "examCodes":
            normalized, review = _normalize_exam_codes(_as_list(value), allowed_exam_codes)
            value = normalized
            result.review_items.extend(review)
            if review:
                record_change = True
        elif key == "author":
            normalized, review = _normalize_author(
                str(value), schema, normalized_categories or []
            )
            value = normalized
            result.review_items.extend(review)
            if review:
                record_change = True
        elif key == "date":
            value = _as_date(value, today)

        _check_length(key, value, spec, result)

        if record_change:
            result.changes.append(
                {
                    "field": key,
                    "before": _format_value(values.get(key)) if present else "(missing)",
                    "after": _format_value(value),
                    "reason": reason_override or _reason_for(key, present),
                }
            )
        emitted.append(_emit(spec, value))
        emitted_keys.append(key)

    known = set(schema.field_map) | set(schema.aliases)
    unknown = [key for key in values if key not in known]
    for key in unknown:
        if nested_preserve and key in nested_preserve:
            raw = _extract_raw_field(doc.frontmatter, key)
            if raw is not None:
                emitted.append(raw)
                continue
        emitted.append(_emit_unknown(key, values[key]))
        result.review_items.append(f"unrecognized field '{key}' preserved; review the schema")

    present_order = [key for key in values if key in schema.field_map]
    if emitted_keys != present_order:
        result.changes.append(
            {
                "field": "(order)",
                "before": ", ".join(present_order) or "(none)",
                "after": ", ".join(emitted_keys) or "(none)",
                "reason": "fields reordered to the schema sequence",
            }
        )

    frontmatter = "\n".join(emitted)
    result.new_text = _render(frontmatter, doc.body)
    if result.new_text == doc.render():
        result.notes.clear()
        result.changes.clear()
    elif not result.changes:
        result.changes.append(
            {
                "field": "(format)",
                "before": "(original formatting)",
                "after": "(normalized)",
                "reason": "quoted string values and normalized spacing for consistency",
            }
        )
    return result


def _value_before(frontmatter: str, key: str) -> str:
    values, _, _ = parse_frontmatter(frontmatter)
    if key in values:
        return _format_value(values[key])
    return "(missing)"


def _reason_for(key: str, present: bool) -> str:
    if not present:
        return "missing required field"
    reasons = {
        "slug": "slug must match the filename",
        "order": "normalized order",
        "categories": "normalized categories",
        "examCodes": "invalid exam code removed",
        "author": "author not approved",
        "date": "normalized date",
        "type": "field must be a fixed value",
        "imagePosition": "field must be a fixed value",
    }
    return reasons.get(key, "normalized value")


def _resolve_value(
    spec: FieldSpec,
    values: dict,
    schema: FrontmatterSchema,
    slug: str,
    body: str,
    kind: str,
    today: str,
    result: FrontmatterResult,
) -> tuple[object | None, bool]:
    key = spec.key
    if key in values:
        raw = values[key]
        if spec.type == "int":
            parsed = _as_int(raw)
            if parsed is None:
                result.warnings.append(f"field '{key}' is not an integer; using {DEFAULT_ORDER}")
                return DEFAULT_ORDER, True
            return parsed, False
        if spec.type == "list":
            return _as_list(raw), False
        if spec.type == "date":
            return _as_date(raw, today), False
        if isinstance(raw, (list, dict)):
            return raw, False
        return str(raw).strip(), False

    if not spec.required:
        return None, False

    if spec.default is not None:
        result.notes.append(f"added required field '{key}'")
        return spec.default, True

    fallback = _derive_value(spec, schema, slug, body, values, today)
    if fallback is None:
        result.warnings.append(f"could not infer required field '{key}'")
        return None, False
    result.notes.append(f"added required field '{key}'")
    result.review_items.append(f"'{key}' was missing and was inferred; verify it")
    return fallback, True


def _derive_value(
    spec: FieldSpec,
    schema: FrontmatterSchema,
    slug: str,
    body: str,
    values: dict,
    today: str,
) -> object | None:
    key = spec.key
    if key == "slug":
        return slug
    if key == "title":
        return _first_h1(body) or _humanize(slug)
    if key == "seoTitle":
        title = str(values.get("title") or _first_h1(body) or _humanize(slug))
        return title[:70].rstrip()
    if key == "seoDescription":
        paragraph = _first_paragraph(body) or _humanize(slug)
        return _trim_to(paragraph, 160)
    if key == "date":
        return today
    if key == "order":
        return DEFAULT_ORDER
    if key == "type":
        return "post"
    if key == "imagePosition":
        return "top"
    if key == "author":
        categories = _normalize_categories(_as_list(values.get("categories")), schema)[0]
        if not categories:
            categories = _infer_categories(schema, slug, values, body)
        return _infer_author(schema, categories)
    if key == "categories":
        return _infer_categories(schema, slug, values, body)
    if key == "examCodes":
        return []
    return None


def _format_value(value: object) -> str:
    if isinstance(value, list):
        return ", ".join(str(item) for item in value) if value else "(empty)"
    return str(value)
