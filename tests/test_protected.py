"""Protected-region tests: fenced/inline code and URLs are never rewritten."""

from __future__ import annotations

from contentos.core.protected import (
    DEFAULT_KINDS,
    mask,
    mask_urls,
    protected_spans,
    replace_guarded,
)


def test_fenced_block_is_protected():
    text = "Rename ACME to X.\n\n```python\nACME = 'ACME'\n```\n\nMore ACME.\n"
    spans = protected_spans(text)
    assert spans
    masked = mask(text)
    assert "ACME" in masked  # the two prose mentions survive (mask keeps offsets, blanks spans)
    assert "ACME = " not in masked
    result = replace_guarded(text, "ACME", "ACME2")
    assert "```python\nACME = 'ACME'\n```" in result
    assert result.count("ACME2") == 2


def test_tilde_fence_is_protected():
    text = "before\n~~~\nsecret token XYZ\n~~~\nafter XYZ\n"
    result = replace_guarded(text, "XYZ", "ABC")
    assert "secret token XYZ" in result
    assert result.endswith("after ABC\n")


def test_inline_code_is_protected():
    text = "Pass the `ACME_TOKEN` value, then ACME remains.\n"
    result = replace_guarded(text, "ACME", "NEW")
    assert "`ACME_TOKEN`" in result
    assert "then NEW remains." in result


def test_urls_are_protected():
    text = "See [exam](https://ex.com/acme-cert) and https://ex.com/acme for ACME.\n"
    result = replace_guarded(text, "acme", "beta", case_insensitive=True)
    assert "https://ex.com/acme-cert" in result
    assert "https://ex.com/acme" in result
    assert "for beta." in result


def test_url_spans_and_mask_urls():
    text = "a [link](https://x.example/foo) b https://y.example/bar c"
    assert len(protected_spans(text, ("urls",))) == 2
    masked = mask_urls(text)
    assert len(masked) == len(text)
    assert "https://x.example/foo" not in masked
    assert "a [link](" in masked  # only the target is blanked


def test_mask_preserves_length_and_newlines():
    text = "line one `code`\nline two ```\nblock\n```\n"
    masked = mask(text)
    assert len(masked) == len(text)
    assert masked.count("\n") == text.count("\n")


def test_replace_guarded_respects_kinds():
    text = "`ACME` sample\n"
    only_urls = replace_guarded(text, "ACME", "NEW", kinds=("urls",))
    assert only_urls == "`NEW` sample\n"
    all_kinds = replace_guarded(text, "ACME", "NEW", kinds=DEFAULT_KINDS)
    assert all_kinds == text


def test_replace_guarded_idempotent_after_replacement():
    text = "Rename ACME now, not ACME soon.\n"
    once = replace_guarded(text, "ACME", "ACME2")
    twice = replace_guarded(once, "ACME", "ACME2")
    assert once == twice
