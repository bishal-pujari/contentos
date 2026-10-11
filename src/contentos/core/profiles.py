"""Content-type profiles and folder resolution.

Every agent used to assume its input was a folder of blog posts with a hard-coded
prompt. This module makes the content type explicit: a single manifest
(``prompts/content_types.json``) describes each type (blog, study-plan, roadmap,
exam-faq and the learn subtypes) and the prompt files, grounding mode and frontmatter
behaviour it needs.

Folder resolution follows the plan:

* an explicit ``--content-type`` always wins;
* otherwise the leaf folder name selects the type (``blogs``, ``study-plans``,
  ``roadmaps``, ``exam-faq``, ``concept``, ``eli10``, ``blueprint``);
* for learn subtypes the parent folder (when it is not ``learn``) is the exam slug,
  with a fallback to frontmatter / the fact registry via :func:`exam_binding_value`.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

import yaml

DEFAULT_MANIFEST_PATH = os.path.join("prompts", "content_types.json")

GROUND_AUTO = "auto"
GROUND_ALWAYS = "always"

FRONTMATTER_NORMALIZE = "normalize"
FRONTMATTER_GENERATE = "generate"
FRONTMATTER_SKIP = "skip"
FRONTMATTER_SEO_ONLY = "seo_only"

VALID_GROUND = (GROUND_AUTO, GROUND_ALWAYS)
VALID_FRONTMATTER = (
    FRONTMATTER_NORMALIZE,
    FRONTMATTER_GENERATE,
    FRONTMATTER_SKIP,
    FRONTMATTER_SEO_ONLY,
)

# Leaf-folder spellings that should resolve to a type whose manifest ``dir`` differs.
_FOLDER_ALIASES = {
    "blog": "blog",
    "blogs": "blog",
    "plan": "study-plan",
    "plans": "study-plan",
    "study_plan": "study-plan",
    "studyplan": "study-plan",
    "study-plans": "study-plan",
    "exam_faq": "exam-faq",
    "examfaq": "exam-faq",
}


@dataclass
class FaqConfig:
    """FAQ behaviour for a content type."""

    applies: bool = False
    heading: str = ""
    questions: int = 0
    question_style: str = ""
    document_is_faq: bool = False


@dataclass
class ContentProfile:
    """Everything an agent needs for one content type."""

    name: str
    dir: str = ""
    family: str | None = None
    rating_prompt: str | None = None
    corrections_prompt: str | None = None
    frontmatter_prompt: str | None = None
    facts_prompt: str | None = None
    seo_prompt: str | None = None
    exam_binding: tuple[str, ...] = ()
    ground: str = GROUND_AUTO
    frontmatter: str = FRONTMATTER_NORMALIZE
    faq: FaqConfig = field(default_factory=FaqConfig)
    strip_links: bool = True
    vendor_contamination: bool = True
    nested_preserve: tuple[str, ...] = ()
    link_field: str | None = None
    link_source: str | None = None
    exclude_fields: tuple[str, ...] = ()

    @property
    def is_learn(self) -> bool:
        return self.family == "learn"

    @property
    def ground_always(self) -> bool:
        return self.ground == GROUND_ALWAYS


@dataclass
class FamilyDef:
    """A content family (currently only ``learn``)."""

    name: str
    root: str
    layout: str = ""
    subtypes: tuple[str, ...] = ()


@dataclass
class ContentTypeManifest:
    """The parsed ``content_types.json``."""

    version: int
    families: dict[str, FamilyDef]
    types: dict[str, ContentProfile]
    path: str = ""

    def profile(self, content_type: str) -> ContentProfile | None:
        return self.types.get(content_type)

    def type_for_dir(self, leaf: str) -> str | None:
        """Return the type whose leaf folder is ``leaf``, if any.

        An ``output``/``outputs`` prefix is ignored so a generated folder (``output-plans``)
        still resolves to its content type (``study-plan``).
        """
        for candidate in _dir_candidates(leaf):
            name = _normalize_type_name(candidate)
            if name in self.types:
                return name
            alias = _FOLDER_ALIASES.get(name)
            if alias in self.types:
                return alias
            for type_name, profile in self.types.items():
                if profile.dir and _normalize_type_name(profile.dir) == name:
                    return type_name
        return None


@dataclass
class ResolvedProfile:
    """A content type resolved for a concrete input folder."""

    profile: ContentProfile
    content_type: str
    exam_slug: str | None = None

    @property
    def family(self) -> str | None:
        return self.profile.family


def load_manifest(path: str = DEFAULT_MANIFEST_PATH) -> ContentTypeManifest:
    """Load and validate the content-type manifest (raises ``FileNotFoundError``)."""
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid content-type manifest {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise TypeError(f"Content-type manifest must be a JSON object: {path}")

    families: dict[str, FamilyDef] = {}
    for name, config in (raw.get("families") or {}).items():
        config = config or {}
        families[name] = FamilyDef(
            name=name,
            root=str(config.get("root", name) or name),
            layout=str(config.get("layout", "") or ""),
            subtypes=tuple(str(item) for item in (config.get("subtypes") or ())),
        )

    types: dict[str, ContentProfile] = {}
    for name, config in (raw.get("types") or {}).items():
        types[name] = _profile_from_dict(name, config or {})

    return ContentTypeManifest(
        version=int(raw.get("version", 1)),
        families=families,
        types=types,
        path=path,
    )


def _profile_from_dict(name: str, config: dict) -> ContentProfile:
    faq_raw = config.get("faq") or {}
    faq = FaqConfig(
        applies=bool(faq_raw.get("applies", False)),
        heading=str(faq_raw.get("heading", "") or ""),
        questions=int(faq_raw.get("questions", 0) or 0),
        question_style=str(faq_raw.get("question_style", "") or ""),
        document_is_faq=bool(faq_raw.get("document_is_faq", False)),
    )
    ground = str(config.get("ground", GROUND_AUTO) or GROUND_AUTO)
    if ground not in VALID_GROUND:
        raise ValueError(f"Content type '{name}' has invalid ground '{ground}'")
    frontmatter = str(config.get("frontmatter", FRONTMATTER_NORMALIZE) or FRONTMATTER_NORMALIZE)
    if frontmatter not in VALID_FRONTMATTER:
        raise ValueError(
            f"Content type '{name}' has invalid frontmatter mode '{frontmatter}'"
        )
    return ContentProfile(
        name=name,
        dir=str(config.get("dir", "") or ""),
        family=config.get("family"),
        rating_prompt=_optional_str(config.get("rating_prompt")),
        corrections_prompt=_optional_str(config.get("corrections_prompt")),
        frontmatter_prompt=_optional_str(config.get("frontmatter_prompt")),
        facts_prompt=_optional_str(config.get("facts_prompt")),
        seo_prompt=_optional_str(config.get("seo_prompt")),
        exam_binding=tuple(str(item) for item in (config.get("exam_binding") or ())),
        ground=ground,
        frontmatter=frontmatter,
        faq=faq,
        strip_links=bool(config.get("strip_links", True)),
        vendor_contamination=bool(config.get("vendor_contamination", True)),
        nested_preserve=tuple(str(item) for item in (config.get("nested_preserve") or ())),
        link_field=_optional_str(config.get("link_field")),
        link_source=_optional_str(config.get("link_source")),
        exclude_fields=tuple(str(item) for item in (config.get("exclude_fields") or ())),
    )


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


def _normalize_type_name(value: str) -> str:
    return str(value).strip().lower().replace("_", "-")


def _dir_candidates(leaf: str) -> list[str]:
    """The leaf folder plus, for a generated output folder, the name without its prefix."""
    candidates = [leaf]
    stripped = re.sub(r"^outputs?[-_ ]?", "", str(leaf), flags=re.IGNORECASE)
    if stripped and stripped != leaf:
        candidates.append(stripped)
    return candidates


def _leaf(path: str) -> str:
    return os.path.basename(os.path.normpath(path))


def _parent_dir(path: str) -> str:
    return os.path.basename(os.path.dirname(os.path.normpath(path)))


def resolve_profile(
    input_dir: str,
    content_type: str | None = None,
    manifest: ContentTypeManifest | None = None,
) -> ResolvedProfile:
    """Resolve the content type (and learn exam folder) for ``input_dir``.

    An explicit ``content_type`` wins; otherwise the leaf folder selects the type.
    Learn subtypes resolve the exam slug from the parent folder when it is not the
    family root (``learn``), so both ``learn/<exam>/concept`` and the current
    ``learn/concept`` layout work.
    """
    manifest = manifest if manifest is not None else load_manifest()
    leaf = _leaf(input_dir)

    if content_type:
        name = content_type
    else:
        name = manifest.type_for_dir(leaf)
    if name is None:
        known = ", ".join(sorted(manifest.types)) or "(none)"
        raise ValueError(
            f"Could not infer a content type from '{input_dir}'; known types: {known}. "
            "Pass --content-type or --profiles."
        )
    profile = manifest.types.get(name)
    if profile is None:
        known = ", ".join(sorted(manifest.types)) or "(none)"
        raise ValueError(f"Unknown content type '{name}'; known types: {known}")

    exam_slug = _exam_from_folders(profile, input_dir, manifest)
    return ResolvedProfile(profile=profile, content_type=name, exam_slug=exam_slug)


def _exam_from_folders(
    profile: ContentProfile, input_dir: str, manifest: ContentTypeManifest
) -> str | None:
    if not profile.family:
        return None
    family = manifest.families.get(profile.family)
    root = family.root if family else profile.family
    parent = _parent_dir(input_dir)
    if parent and parent != root:
        return parent
    return None


def exam_binding_value(
    profile: ContentProfile,
    doc,
    path: str,
    facts_registry: object | None = None,
) -> str | None:
    """Return the exam slug/code that binds ``doc``/``path`` to a fact file.

    ``profile.exam_binding`` lists the sources to try in order: ``exam_folder`` (the
    folder above the subtype directory), ``examCode`` / ``examCodes`` / ``exam_id``
    (frontmatter), ``filename_slug`` and ``categories`` (first entry). When a fact
    registry is supplied, a candidate it knows is preferred over one it does not.
    """
    values = _frontmatter_values(doc)
    candidates: list[str] = []
    for binding in profile.exam_binding:
        candidates.extend(_binding_candidates(binding, profile, path, values))
    if not candidates:
        return None
    if facts_registry is not None:
        for candidate in candidates:
            if _known_exam(facts_registry, candidate):
                return candidate
    return candidates[0]


def _binding_candidates(
    binding: str, profile: ContentProfile, path: str, values: dict
) -> list[str]:
    if binding == "exam_folder":
        parent = _parent_dir(path)
        grand = _parent_dir(os.path.dirname(os.path.normpath(path)))
        candidates = []
        for folder in (grand, parent):
            if not folder:
                continue
            if folder in {"learn", profile.dir, profile.family}:
                continue
            candidates.append(folder)
        return candidates
    if binding == "filename_slug":
        return [os.path.splitext(os.path.basename(path))[0]]
    if binding == "categories":
        categories = values.get("categories")
        if isinstance(categories, list) and categories:
            return [str(categories[0])]
        if isinstance(categories, str) and categories.strip():
            return [categories.strip()]
        return []
    if binding in {"examCode", "examCodes", "exam_id", "examId"}:
        raw = values.get(binding)
        if isinstance(raw, list):
            return [str(item) for item in raw if item is not None]
        if raw is not None:
            return [str(raw)]
    return []


def _frontmatter_values(doc) -> dict:
    frontmatter = getattr(doc, "frontmatter", "") or ""
    if not frontmatter:
        return {}
    try:
        data = yaml.safe_load(frontmatter)
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def _known_exam(registry: object, value: str) -> bool:
    for_code = getattr(registry, "for_code", None)
    if callable(for_code):
        return for_code(value) is not None
    if isinstance(registry, dict):
        return any(str(key).upper() == value.upper() for key in registry)
    if isinstance(registry, (list, tuple, set)):
        return any(str(item).upper() == value.upper() for item in registry)
    return False
