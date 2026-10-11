"""Content-type manifest/profile resolution tests (no network)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from contentos.core.loaders import parse_document
from contentos.core.profiles import (
    DEFAULT_MANIFEST_PATH,
    exam_binding_value,
    load_manifest,
    resolve_profile,
)

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / DEFAULT_MANIFEST_PATH


@pytest.fixture(scope="module")
def manifest():
    return load_manifest(str(MANIFEST_PATH))


def test_manifest_loads_every_type(manifest):
    assert manifest.version == 1
    assert set(manifest.types) == {
        "blog",
        "study-plan",
        "roadmap",
        "exam-faq",
        "concept",
        "eli10",
        "blueprint",
    }
    assert "learn" in manifest.families
    assert manifest.families["learn"].subtypes == ("concept", "eli10", "blueprint")
    blog = manifest.profile("blog")
    assert blog is not None
    assert blog.dir == "blogs"
    assert blog.faq.applies and blog.faq.questions == 4
    assert blog.ground == "auto"
    assert blog.frontmatter == "normalize"
    assert manifest.profile("exam-faq").frontmatter == "skip"
    assert manifest.profile("blueprint").frontmatter == "seo_only"
    assert "parentConcept" in manifest.profile("blueprint").exclude_fields
    assert manifest.profile("eli10").link_field == "parentConcept"


def test_profile_flags_for_live_families(manifest):
    assert manifest.profile("study-plan").ground_always is True
    assert manifest.profile("exam-faq").ground_always is True
    assert manifest.profile("blog").ground_always is False
    assert manifest.profile("roadmap").ground_always is False

    assert manifest.profile("roadmap").faq.applies is False
    assert manifest.profile("exam-faq").faq.applies is False
    assert manifest.profile("exam-faq").faq.document_is_faq is True
    assert manifest.profile("study-plan").faq.applies is True

    assert manifest.profile("exam-faq").strip_links is False
    assert manifest.profile("roadmap").strip_links is True

    assert manifest.profile("roadmap").vendor_contamination is False
    assert manifest.profile("study-plan").vendor_contamination is True
    assert "skills" in manifest.profile("roadmap").nested_preserve

    assert manifest.profile("study-plan").exam_binding == ("examCode",)
    assert manifest.profile("exam-faq").exam_binding == ("filename_slug",)


def test_resolve_flat_folders(manifest):
    assert resolve_profile("blogs", None, manifest).content_type == "blog"
    assert resolve_profile("blog", None, manifest).content_type == "blog"
    assert resolve_profile("study-plans", None, manifest).content_type == "study-plan"
    assert resolve_profile("roadmaps", None, manifest).content_type == "roadmap"
    assert resolve_profile("exam-faq", None, manifest).content_type == "exam-faq"
    assert resolve_profile("blogs/", None, manifest).content_type == "blog"


def test_resolve_output_folders(manifest):
    assert resolve_profile("output-plans", None, manifest).content_type == "study-plan"
    assert manifest.type_for_dir("output_plans") == "study-plan"
    assert manifest.type_for_dir("output-plans") == "study-plan"


def test_learn_layout_with_and_without_exam(manifest):
    flat = resolve_profile("learn/concept", None, manifest)
    assert flat.content_type == "concept"
    assert flat.exam_slug is None
    assert flat.family == "learn"

    nested = resolve_profile("learn/my-exam/eli10", None, manifest)
    assert nested.content_type == "eli10"
    assert nested.exam_slug == "my-exam"
    assert nested.profile.ground == "always"


def test_explicit_content_type_wins(manifest):
    resolved = resolve_profile("learn/concept", "blueprint", manifest)
    assert resolved.content_type == "blueprint"


def test_unknown_folder_raises(manifest):
    with pytest.raises(ValueError):
        resolve_profile("some/unrelated/folder", None, manifest)


def test_missing_manifest_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_manifest(str(tmp_path / "nope.json"))


def test_invalid_manifest_raises(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{ not json", encoding="utf-8")
    with pytest.raises(ValueError):
        load_manifest(str(bad))


def test_invalid_ground_rejected(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(
        json.dumps({"version": 1, "types": {"blog": {"ground": "sometimes"}}}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_manifest(str(bad))


def _doc(frontmatter: str):
    return parse_document(f"---\n{frontmatter}\n---\n# H\n", "x.mdx")


def test_exam_binding_examcodes(manifest):
    profile = manifest.profile("blog")
    doc = _doc('examCodes: ["AI-103", "AZ-104"]\ntitle: "T"')
    assert exam_binding_value(profile, doc, "blogs/x.mdx") == "AI-103"


def test_exam_binding_falls_back_to_categories(manifest):
    profile = manifest.profile("concept")
    doc = _doc('categories: ["cca-f"]\ntitle: "T"')
    assert exam_binding_value(profile, doc, "learn/cca-f/concept/x.mdx") == "cca-f"


def test_exam_binding_exam_folder(manifest):
    profile = manifest.profile("blueprint")
    doc = _doc('exam_id: "cca-f"\ntitle: "T"')
    value = exam_binding_value(profile, doc, "learn/cca-f/blueprint/x.mdx")
    assert value == "cca-f"


def test_exam_binding_filename_slug(manifest):
    profile = manifest.profile("exam-faq")
    doc = _doc('title: "T"')
    assert exam_binding_value(profile, doc, "exam-faq/az-104.mdx") == "az-104"


def test_exam_binding_prefers_known_exam(manifest):
    profile = manifest.profile("blog")

    class Registry:
        def for_code(self, code):
            return object() if str(code).upper() == "AZ-104" else None

    doc = _doc('examCodes: ["UNKNOWN", "AZ-104"]\ntitle: "T"')
    assert exam_binding_value(profile, doc, "blogs/x.mdx", Registry()) == "AZ-104"
