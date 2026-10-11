"""Agent 1: strip_links — remove hyperlinks and link-header blocks from content.

Pipeline per document:
  1. Remove embed components (<PracticeExamEmbed .../>) — dynamic link blocks.
  2. Remove "label + link" blocks (e.g. "Related Exams:" followed by only link lines).
  3. Remove standalone link lines (a line that is only a markdown link / image).
  4. Inline links: replace [text](url) with plain anchor text.
  5. Bare URLs: remove the URL token, keep the surrounding sentence intact.
  6. Cleanup: collapse 3+ blank lines, drop orphaned horizontal rules.

LLM pass (optional, Gemini / DeepSeek) is intentionally NOT used for rewriting —
it may only *select* ambiguous lines for removal. Implemented behind a flag in a
later iteration; the rules above cover the known corpus patterns.
"""

from __future__ import annotations

import re

from contentos.core.loaders import ContentDocument
from contentos.core.protected import protected_spans, sub_guarded

# --- Patterns ---------------------------------------------------------------

# Allow an optional markdown list bullet ("- <PracticeExamEmbed .../>") and any leading
# indentation, since roadmaps nest the embed under list items.
EMBED_RE = re.compile(
    r"^\s*(?:[-*+]\s+)?<[A-Z][A-Za-z0-9]*(Embed|Link|CTA|Banner)[^>]*/>\s*$"
)

# A markdown link: [text](url). Images: ![alt](src). Tolerates an optional space
# between "](" and an angle-bracketed URL with spaces, because generated content
# sometimes emits "[anchor] (/path)" which CommonMark would not treat as a link.
MD_LINK_RE = re.compile(
    r"!?\[([^\]]*)\]\s*\(\s*(?:<[^>]*>|[^)\s]+)(?:\s+\"[^\"]*\")?\s*\)"
)
MD_LINK_FULL_LINE_RE = re.compile(r"^\s*!?\[[^\]]*\]\s*\([^)]+\)\s*$")
IMAGE_RE = re.compile(r"!\[[^\]]*\]\s*\([^)]*\)")

# Label lines that act as headers for link blocks, e.g. "Related Exam:" / "Further Reading:"
LABEL_LINE_RE = re.compile(
    r"^\s*(?:#{2,6}\s+|\*\*)?"
    r"(Related|Further|Recommended|Suggested|See also|Resources|Read more|Useful)"
    r"[^:\n]{0,60}:\s*(?:\*\*)?\s*$",
    re.IGNORECASE,
)

# Bare URLs (not inside markdown link syntax) — http(s)://...
BARE_URL_RE = re.compile(r"https?://[^\s)\]>\"']+")

# Root-relative URLs embedded in prose (/exams/..., /blog/..., /study-plans/...).
# The leading ``(?<![\w)\]])`` avoids matching dates or fractions like ``24/7`` and the
# lookahead requires a path (at least two segments) so a lone ``/`` is never touched.
REL_URL_RE = re.compile(r"(?<![\w)\]])/(?:[a-z0-9][a-z0-9-]*)(?:/[a-z0-9][a-z0-9-]*)+")

HR_LINE_RE = re.compile(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$")
BLANK_RE = re.compile(r"^\s*$")


def strip_frontmatter_embeds(frontmatter: str) -> tuple[str, list[str]]:
    """Remove standalone embed-component lines from the YAML frontmatter.

    Body agents normally leave the frontmatter untouched, but roadmap steps/milestones are
    nested YAML and carry ``<PracticeExamEmbed .../>`` list items. Only a line that is a
    bare embed component (optionally bulleted and indented) is removed; every other
    frontmatter line is preserved byte-for-byte, so this stays surgical.
    """
    if not frontmatter or "<" not in frontmatter:
        return frontmatter, []
    lines = frontmatter.split("\n")
    keep = [True] * len(lines)
    notes: list[str] = []
    for index, line in enumerate(lines):
        if EMBED_RE.match(line):
            keep[index] = False
            notes.append(
                f"L{index + 1}: removed embed component from frontmatter: {line.strip()[:60]}"
            )
    if all(keep):
        return frontmatter, []
    return "\n".join(line for line, kept in zip(lines, keep) if kept), notes


def _fenced_line_indices(body: str) -> set[int]:
    """Line indices inside a fenced code block (never stripped)."""
    spans = protected_spans(body, kinds=("fenced",))
    if not spans:
        return set()
    fenced: set[int] = set()
    position = 0
    for index, line in enumerate(body.split("\n")):
        if any(start <= position < end for start, end in spans):
            fenced.add(index)
        position += len(line) + 1
    return fenced


def strip_links(doc: ContentDocument) -> tuple[str, list[str]]:
    """Return (new_body, notes). Frontmatter is never touched."""
    lines = doc.body.split("\n")
    fenced = _fenced_line_indices(doc.body)
    keep = [True] * len(lines)
    notes: list[str] = []

    # Pass 1: embed components
    for i, line in enumerate(lines):
        if i in fenced:
            continue
        if EMBED_RE.match(line):
            keep[i] = False
            notes.append(f"L{i + 1}: removed embed component: {line.strip()[:60]}")

    # Pass 2: label + following block of link-only lines
    for i, line in enumerate(lines):
        if i in fenced or not keep[i] or not LABEL_LINE_RE.match(line):
            continue
        # Collect following block: blank lines + link-only lines
        j = i + 1
        block_links = 0
        while j < len(lines):
            if BLANK_RE.match(lines[j]):
                j += 1
                continue
            if MD_LINK_FULL_LINE_RE.match(lines[j]):
                block_links += 1
                j += 1
                continue
            break
        if block_links > 0:
            keep[i] = False
            notes.append(f"L{i + 1}: removed link-block label: {line.strip()[:60]}")
            for k in range(i + 1, j):
                if MD_LINK_FULL_LINE_RE.match(lines[k]):
                    keep[k] = False
                    notes.append(f"L{k + 1}: removed link line under label: {lines[k].strip()[:60]}")

    # Pass 3: standalone link lines not already removed
    for i, line in enumerate(lines):
        if i not in fenced and keep[i] and MD_LINK_FULL_LINE_RE.match(line):
            keep[i] = False
            notes.append(f"L{i + 1}: removed standalone link line: {line.strip()[:60]}")

    # Pass 4 + 5: inline links and bare URLs, per line. Fenced blocks and inline code
    # spans are protected, so a link inside `code` is never touched.
    out_lines: list[str] = []
    for index, line in enumerate(lines):
        if not keep[index]:
            continue
        if index in fenced:
            out_lines.append(line)
            continue
        original = line
        kinds = ("inline",)
        # Images: drop entirely (syntax usually just decorative)
        line = sub_guarded(line, IMAGE_RE, "", kinds=kinds)
        # Inline links: keep the anchor text (brackets and target removed)
        line = sub_guarded(line, MD_LINK_RE, lambda m: m.group(1), kinds=kinds)
        # Bare URLs and root-relative URLs: remove the token only — sentence stays intact
        line = sub_guarded(line, BARE_URL_RE, "", kinds=kinds)
        line = sub_guarded(line, REL_URL_RE, "", kinds=kinds)
        # Tidy artifacts: now-empty parentheses, doubled spaces, space before punctuation
        line = re.sub(r"\(\s*\)", "", line)
        line = re.sub(r" {2,}", " ", line)
        line = re.sub(r"\s+([.,;:!?])", r"\1", line)
        # If the line became empty or pure punctuation, drop it
        if original != line and not line.strip():
            notes.append(f"dropped line emptied by URL removal: {original.strip()[:60]}")
            continue
        if original != line:
            notes.append(f"unlinked inline: {original.strip()[:60]}")
        out_lines.append(line)

    # Pass 6a: remove orphaned horizontal rules (rule followed only by blank/removed content at EOF)
    while out_lines and HR_LINE_RE.match(out_lines[-1]):
        out_lines.pop()
        notes.append("removed orphaned trailing horizontal rule")
        while out_lines and BLANK_RE.match(out_lines[-1]):
            out_lines.pop()

    # Pass 6b: collapse 3+ consecutive blank lines to 1 blank line
    final: list[str] = []
    blanks = 0
    for line in out_lines:
        if BLANK_RE.match(line):
            blanks += 1
            if blanks == 1:
                final.append("")
        else:
            blanks = 0
            final.append(line)

    body = "\n".join(final).strip("\n") + "\n"
    return body, notes
