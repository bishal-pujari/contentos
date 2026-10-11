"""Content loading: frontmatter-safe reading of MDX/Markdown files."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---[ \t]*\r?\n?", re.DOTALL)

SUPPORTED_EXTENSIONS = {".md", ".mdx", ".txt"}


@dataclass
class ContentDocument:
    """A content file split into frontmatter (untouchable) and body."""

    path: str
    frontmatter: str  # raw YAML text, "" if none
    body: str
    has_frontmatter: bool = field(default=False)

    def render(self) -> str:
        if self.has_frontmatter:
            return f"---\n{self.frontmatter}\n---\n{self.body}"
        return self.body


def load_document(path: str) -> ContentDocument:
    # ``utf-8-sig`` drops a leading UTF-8 BOM, which some source files carry. A BOM
    # left in place would sit before the opening ``---`` and make the file look like
    # it has no frontmatter, so the frontmatter agent would generate a second block.
    with open(path, encoding="utf-8-sig") as f:
        raw = f.read()
    return parse_document(raw, path)


def parse_document(raw: str, path: str = "") -> ContentDocument:
    """Split an in-memory document string into frontmatter + body."""
    raw = raw.removeprefix("\ufeff")
    m = FRONTMATTER_RE.match(raw)
    if m:
        return ContentDocument(
            path=path,
            frontmatter=m.group(1),
            body=raw[m.end():],
            has_frontmatter=True,
        )
    return ContentDocument(path=path, frontmatter="", body=raw)


def discover_documents(directory: str) -> list[str]:
    """Return sorted list of content file paths under a directory."""
    import os

    if not os.path.isdir(directory):
        raise FileNotFoundError(f"Input directory not found: {directory}")

    found: list[str] = []
    for root, _dirs, files in os.walk(directory):
        for name in sorted(files):
            if os.path.splitext(name)[1].lower() in SUPPORTED_EXTENSIONS:
                found.append(os.path.join(root, name))
    return sorted(found)
