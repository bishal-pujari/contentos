"""Protected text regions shared by every contentos agent.

Learning content and blog posts contain fenced code blocks (``` / ~~~), inline code
(`` `code` ``) and URLs (markdown link targets and bare links). None of the agents may
match or rewrite text inside those regions: a service rename must not corrupt a model
ID, an exam code must not be read out of a slug, and a de-slop pass must never edit a
code sample.

This module is the single source of truth for "protected spans". It owns the URL
helpers the fact checker used to keep locally (``url_spans`` / ``mask_urls``) so the
code-fence and inline-code protection can be layered on top.

Public API:

* :func:`protected_spans` -- spans of protected regions.
* :func:`mask` -- equal-length copy with protected regions blanked, for scanning
  (word counts, regex probing) without seeing protected text.
* :func:`replace_guarded` -- literal/word-boundary replacement that skips matches
  inside protected regions.
"""

from __future__ import annotations

import re

# Kinds understood by protected_spans(); the default protects everything.
FENCED = "fenced"
INLINE = "inline"
URLS = "urls"
DEFAULT_KINDS: tuple[str, ...] = (FENCED, INLINE, URLS)

_FENCE_OPEN_RE = re.compile(r"[ \t]{0,3}(`{3,}|~{3,})")
_MARKDOWN_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
_BARE_URL_RE = re.compile(r"https?://\S+")
_INLINE_CODE_RE = re.compile(r"(`+)(.+?)\1", re.DOTALL)


def _line_starts(text: str) -> list[int]:
    starts: list[int] = []
    position = 0
    for line in text.splitlines(keepends=True):
        starts.append(position)
        position += len(line)
    return starts


def _fence_spans(text: str) -> list[tuple[int, int]]:
    """Spans of fenced code blocks (``` or ~~~ opening and closing fences)."""
    lines = text.splitlines(keepends=True)
    starts = _line_starts(text)
    total = len(lines)
    spans: list[tuple[int, int]] = []
    index = 0
    while index < total:
        opener = _FENCE_OPEN_RE.match(lines[index])
        if opener is None:
            index += 1
            continue
        fence = opener.group(1)
        char = fence[0]
        minimum = len(fence)
        close_re = re.compile(
            rf"[ \t]{{0,3}}{re.escape(char)}{{{minimum},}}[ \t]*\r?\n?$"
        )
        end: int | None = None
        scan = index + 1
        while scan < total:
            if close_re.match(lines[scan]):
                end = starts[scan] + len(lines[scan])
                break
            scan += 1
        if end is None:
            end = len(text)
            scan = total
        spans.append((starts[index], end))
        index = scan + 1
    return spans


def _inline_spans(text: str) -> list[tuple[int, int]]:
    """Spans of inline code (`` `code` ``), never crossing a fenced block boundary."""
    return [match.span() for match in _INLINE_CODE_RE.finditer(text)]


def url_spans(text: str) -> list[tuple[int, int]]:
    """Spans of markdown link targets and bare URLs."""
    spans: list[tuple[int, int]] = []
    for match in _MARKDOWN_LINK_RE.finditer(text):
        spans.append(match.span(1))
    for match in _BARE_URL_RE.finditer(text):
        spans.append(match.span())
    return spans


def _merge(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if start >= end:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def protected_spans(
    text: str, kinds: tuple[str, ...] = DEFAULT_KINDS
) -> list[tuple[int, int]]:
    """Return the (start, end) spans that must never be matched or rewritten.

    ``kinds`` selects which regions count: ``"fenced"`` (``` / ~~~ blocks),
    ``"inline"`` (`` `code` ``) and ``"urls"`` (link targets and bare URLs).
    """
    if not text:
        return []
    spans: list[tuple[int, int]] = []
    if FENCED in kinds:
        spans.extend(_fence_spans(text))
    if INLINE in kinds:
        spans.extend(_inline_spans(text))
    if URLS in kinds:
        spans.extend(url_spans(text))
    return _merge(spans)


def _overlaps(spans: list[tuple[int, int]], start: int, end: int) -> bool:
    return any(match_start < end and start < match_end for match_start, match_end in spans)


def mask(text: str, kinds: tuple[str, ...] = DEFAULT_KINDS) -> str:
    """Return an equal-length copy with every protected span blanked.

    Newlines are preserved so line numbers and offsets stay identical to ``text``;
    the result is safe to scan but must never be written back to a file.
    """
    if not text:
        return text
    chars = list(text)
    for start, end in protected_spans(text, kinds):
        for index in range(start, end):
            if chars[index] != "\n":
                chars[index] = " "
    return "".join(chars)


def mask_urls(text: str) -> str:
    """Backward-compatible helper: mask only URLs (a slug must not read as content)."""
    return mask(text, (URLS,))


def _boundary_pattern(find: str) -> re.Pattern[str]:
    left = r"\b" if find[:1].isalnum() else ""
    right = r"\b" if find[-1:].isalnum() else ""
    return re.compile(f"{left}{re.escape(find)}{right}")


def replace_guarded(
    text: str,
    find: str,
    replacement: str,
    *,
    case_insensitive: bool = False,
    kinds: tuple[str, ...] = DEFAULT_KINDS,
) -> str:
    """Replace ``find`` with ``replacement``, skipping every protected region.

    Matching is literal by default (word-boundary anchored when the phrase starts and
    ends alphanumerically); ``case_insensitive`` switches to a case-insensitive literal
    match. A match whose span overlaps a fenced block, inline code or URL is left
    untouched.
    """
    if not find:
        return text
    pattern = (
        re.compile(re.escape(find), re.IGNORECASE)
        if case_insensitive
        else _boundary_pattern(find)
    )
    spans = protected_spans(text, kinds)
    if not spans:
        return pattern.sub(replacement, text)
    return pattern.sub(
        lambda match: match.group(0)
        if _overlaps(spans, match.start(), match.end())
        else replacement,
        text,
    )


def count_guarded(
    text: str,
    find: str,
    *,
    case_insensitive: bool = False,
    kinds: tuple[str, ...] = DEFAULT_KINDS,
) -> int:
    """Number of matches of ``find`` that fall outside every protected region."""
    if not find:
        return 0
    pattern = (
        re.compile(re.escape(find), re.IGNORECASE)
        if case_insensitive
        else _boundary_pattern(find)
    )
    spans = protected_spans(text, kinds)
    return sum(
        1
        for match in pattern.finditer(text)
        if not _overlaps(spans, match.start(), match.end())
    )


def sub_guarded(
    text: str,
    pattern: re.Pattern[str],
    replacement,
    *,
    kinds: tuple[str, ...] = DEFAULT_KINDS,
) -> str:
    """Apply a regex substitution only outside every protected region.

    ``replacement`` follows ``re.Pattern.sub`` (a string or a callable). A match whose
    span overlaps a fenced block, inline code or URL is left untouched.
    """
    spans = protected_spans(text, kinds)
    if not spans:
        return pattern.sub(replacement, text)

    def _replace(match: re.Match[str]) -> str:
        if _overlaps(spans, match.start(), match.end()):
            return match.group(0)
        return replacement(match) if callable(replacement) else replacement

    return pattern.sub(_replace, text)
