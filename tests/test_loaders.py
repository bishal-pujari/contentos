"""Document loader tests: BOM and CRLF tolerance in frontmatter splitting."""

from __future__ import annotations

import pytest

from contentos.core.loaders import discover_documents, load_document, parse_document


def test_parse_document_strips_bom():
    text = "\ufeff---\nslug: post\ntitle: T\n---\n# H1\n\nBody\n"
    doc = parse_document(text, "post.mdx")
    assert doc.has_frontmatter
    assert doc.frontmatter == "slug: post\ntitle: T"
    assert doc.body == "# H1\n\nBody\n"
    assert "\ufeff" not in doc.body


def test_parse_document_tolerates_crlf_frontmatter():
    text = "---\r\nslug: post\r\n---\r\n# H1\r\n"
    doc = parse_document(text, "post.mdx")
    assert doc.has_frontmatter
    assert doc.frontmatter == "slug: post"
    assert doc.body == "# H1\r\n"


def test_load_document_strips_bom_from_file(tmp_path):
    path = tmp_path / "post.mdx"
    path.write_bytes(b"\xef\xbb\xbf---\nslug: post\ntitle: T\n---\n# H1\n")
    doc = load_document(str(path))
    assert doc.has_frontmatter
    assert doc.frontmatter == "slug: post\ntitle: T"
    assert doc.body == "# H1\n"


def test_load_document_without_frontmatter_keeps_body(tmp_path):
    path = tmp_path / "plain.mdx"
    path.write_bytes(b"\xef\xbb\xbf# Just a heading\n")
    doc = load_document(str(path))
    assert not doc.has_frontmatter
    assert doc.body == "# Just a heading\n"


def test_discover_documents_missing_directory_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        discover_documents(str(tmp_path / "does-not-exist"))
