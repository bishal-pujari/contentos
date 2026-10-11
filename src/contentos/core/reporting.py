"""Diff reporting for agent runs."""

from __future__ import annotations

import difflib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass
class FileChange:
    path: str
    removed_lines: int = 0
    modified_lines: int = 0
    notes: list[str] = field(default_factory=list)
    diff: str = ""


@dataclass
class RunReport:
    agent: str
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    files_scanned: int = 0
    files_changed: int = 0
    changes: list[FileChange] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    def add(self, change: FileChange) -> None:
        self.changes.append(change)
        if change.diff:
            self.files_changed += 1


def make_diff(path: str, before: str, after: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )


def _stamp(started_at: str) -> str:
    """Microsecond-resolution filename stamp so two runs can never collide."""
    try:
        return datetime.fromisoformat(started_at).strftime("%Y%m%dT%H%M%S%f")
    except ValueError:
        return started_at.replace(":", "").replace("+", "Z")[:19]


def _to_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def load_rating_scores(path: str) -> dict[str, dict[str, float]]:
    """Load ``slug -> {criterion: score}`` from a rating report.

    Tolerant of a malformed report: if ``json.loads`` fails (e.g. a file corrupted by
    two old runs sharing one filename), it scans the text block by block instead. Used
    to target corrections at the posts a criterion actually flagged.
    """
    with open(path, encoding="utf-8") as f:
        text = f.read()

    scores: dict[str, dict[str, float]] = {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict) and isinstance(payload.get("ratings"), list):
        for record in payload["ratings"]:
            slug = record.get("slug")
            if not slug:
                continue
            raw = record.get("scores") or {}
            scores[str(slug)] = {str(key): _to_float(value) for key, value in raw.items()}
        return scores

    for match in re.finditer(r'"slug":\s*"([^"]+)"(.*?)(?="slug":|\Z)', text, re.DOTALL):
        slug, block = match.group(1), match.group(2)
        block_scores = {
            item.group(1): _to_float(item.group(2))
            for item in re.finditer(r'"([a-z_]+)":\s*([0-9]+(?:\.[0-9]+)?)', block)
        }
        if block_scores:
            scores[slug] = block_scores
    return scores


def load_rating_grades(path: str) -> dict[str, dict]:
    """Load ``slug -> {"average": float, "grade": str}`` from a rating report.

    Tolerant of a malformed report (falls back to scanning the text), like
    :func:`load_rating_scores`.
    """
    with open(path, encoding="utf-8") as f:
        text = f.read()

    grades: dict[str, dict] = {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict) and isinstance(payload.get("ratings"), list):
        for record in payload["ratings"]:
            slug = record.get("slug")
            if not slug:
                continue
            grades[str(slug)] = {
                "average": _to_float(record.get("average")),
                "grade": str(record.get("grade") or ""),
            }
        return grades

    for match in re.finditer(r'"slug":\s*"([^"]+)"(.*?)(?="slug":|\Z)', text, re.DOTALL):
        slug, block = match.group(1), match.group(2)
        average = re.search(r'"average":\s*([0-9]+(?:\.[0-9]+)?)', block)
        grade = re.search(r'"grade":\s*"([^"]+)"', block)
        grades[slug] = {
            "average": _to_float(average.group(1)) if average else 0.0,
            "grade": grade.group(1) if grade else "",
        }
    return grades


def _atomic_write_text(path: str, text: str) -> None:
    """Write via a temp file + rename so readers never see a partial file.

    The rename is atomic on the same filesystem, which also stops two runs from
    interleaving into one report and producing unparseable output.
    """
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _atomic_write_json(path: str, payload) -> None:
    _atomic_write_text(path, json.dumps(payload, indent=2, ensure_ascii=False))


@dataclass
class SourceInterlink:
    """Per-source result of the interlink agent."""

    slug: str
    path: str
    primary_exam: str | None = None
    primary_topic: str = ""
    audience: str = ""
    no_exam_found: bool = False
    links: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class InterlinkRunReport:
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    files_scanned: int = 0
    sources: list[SourceInterlink] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    @property
    def files_processed(self) -> int:
        return sum(1 for source in self.sources if not source.error)

    @property
    def total_links(self) -> int:
        return sum(len(source.links) for source in self.sources)

    @property
    def sources_without_exam(self) -> list[str]:
        return [source.slug for source in self.sources if source.no_exam_found and not source.error]


def write_interlink_report(report: InterlinkRunReport, reports_dir: str) -> str:
    """Write the interlink run report (analysis + guardrail warnings) as JSON."""
    os.makedirs(reports_dir, exist_ok=True)
    stamp = _stamp(report.started_at)
    path = os.path.join(reports_dir, f"interlink-{stamp}.json")
    payload = {
        "agent": "interlink",
        "started_at": report.started_at,
        "files_scanned": report.files_scanned,
        "files_processed": report.files_processed,
        "total_links": report.total_links,
        "sources_without_exam": report.sources_without_exam,
        "sources": [
            {
                "slug": source.slug,
                "path": source.path,
                "primary_exam": source.primary_exam,
                "primary_topic": source.primary_topic,
                "audience": source.audience,
                "no_exam_found": source.no_exam_found,
                "link_count": len(source.links),
                "links": source.links,
                "warnings": source.warnings,
                "error": source.error,
            }
            for source in report.sources
        ],
    }
    _atomic_write_json(path, payload)
    return path


@dataclass
class ReviewRunReport:
    """Result of the deterministic interlink reviewer (plus optional LLM redo)."""

    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    mapping_path: str = ""
    output_path: str = ""
    sources_scanned: int = 0
    incomplete_fixed: list[str] = field(default_factory=list)
    anchors_fixed: list[str] = field(default_factory=list)
    reciprocal_dropped: list[str] = field(default_factory=list)
    quizzes_fixed: list[str] = field(default_factory=list)
    promoted_to_related: list[str] = field(default_factory=list)
    redo_sources: list[str] = field(default_factory=list)
    redo_results: list[SourceInterlink] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    @property
    def redo_failed(self) -> list[str]:
        return [source.slug for source in self.redo_results if source.error]


def write_review_report(report: ReviewRunReport, reports_dir: str) -> str:
    """Write the reviewer's repair notes + redo results as JSON."""
    os.makedirs(reports_dir, exist_ok=True)
    stamp = _stamp(report.started_at)
    path = os.path.join(reports_dir, f"review-{stamp}.json")
    payload = {
        "agent": "review_interlinks",
        "started_at": report.started_at,
        "mapping_path": report.mapping_path,
        "output_path": report.output_path,
        "sources_scanned": report.sources_scanned,
        "incomplete_fixed": report.incomplete_fixed,
        "anchors_fixed": report.anchors_fixed,
        "reciprocal_dropped": report.reciprocal_dropped,
        "quizzes_fixed": report.quizzes_fixed,
        "promoted_to_related": report.promoted_to_related,
        "redo_sources": report.redo_sources,
        "redo_results": [
            {
                "slug": source.slug,
                "path": source.path,
                "primary_exam": source.primary_exam,
                "link_count": len(source.links),
                "links": source.links,
                "warnings": source.warnings,
                "error": source.error,
            }
            for source in report.redo_results
        ],
    }
    _atomic_write_json(path, payload)
    return path


@dataclass
class RatingRunReport:
    """Result of the blog rating agent (one record per file)."""

    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    input_dir: str = ""
    rubric: str = ""
    files_scanned: int = 0
    max_searches: int = 0
    searches_used: int = 0
    ratings: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    markdown: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def files_rated(self) -> int:
        return len(self.ratings)

    @property
    def average(self) -> float:
        if not self.ratings:
            return 0.0
        return sum(record["average"] for record in self.ratings) / len(self.ratings)

    @property
    def grade_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in self.ratings:
            counts[record["grade"]] = counts.get(record["grade"], 0) + 1
        return counts

    @property
    def searches(self) -> int:
        return sum(1 for record in self.ratings if record.get("searched"))


def write_rating_report(report: RatingRunReport, reports_dir: str) -> tuple[str, str]:
    """Write the combined markdown report + JSON summary. Returns (json, markdown)."""
    os.makedirs(reports_dir, exist_ok=True)
    stamp = _stamp(report.started_at)
    base = f"rating-{stamp}"

    markdown_path = os.path.join(reports_dir, f"{base}.md")
    markdown_text = report.markdown
    if markdown_text and not markdown_text.endswith("\n"):
        markdown_text += "\n"
    _atomic_write_text(markdown_path, markdown_text)

    json_path = os.path.join(reports_dir, f"{base}.json")
    payload = {
        "agent": "rate_blogs",
        "started_at": report.started_at,
        "input_dir": report.input_dir,
        "rubric": report.rubric,
        "files_scanned": report.files_scanned,
        "files_rated": report.files_rated,
        "average": round(report.average, 2),
        "grade_counts": report.grade_counts,
        "max_web_searches": report.max_searches,
        "search_calls": report.searches_used,
        "web_searches": report.searches,
        "ratings": report.ratings,
        "errors": report.errors,
    }
    _atomic_write_json(json_path, payload)
    return json_path, markdown_path


def write_report(report: RunReport, reports_dir: str) -> tuple[str, str]:
    """Write JSON summary + combined .diff file. Returns their paths."""
    os.makedirs(reports_dir, exist_ok=True)
    stamp = _stamp(report.started_at)
    base = f"{report.agent}-{stamp}"

    diff_path = os.path.join(reports_dir, f"{base}.diff")
    diff_text = ""
    for change in report.changes:
        if change.diff:
            diff_text += change.diff
            if not change.diff.endswith("\n"):
                diff_text += "\n"
    _atomic_write_text(diff_path, diff_text)

    json_path = os.path.join(reports_dir, f"{base}.json")
    payload = {
        "agent": report.agent,
        "started_at": report.started_at,
        "files_scanned": report.files_scanned,
        "files_changed": report.files_changed,
        "changes": [
            {
                "path": c.path,
                "removed_lines": c.removed_lines,
                "modified_lines": c.modified_lines,
                "notes": c.notes,
            }
            for c in report.changes
        ],
    }
    _atomic_write_json(json_path, payload)

    return json_path, diff_path


@dataclass
class CorrectionChange:
    """Per-file result of the correction agent."""

    slug: str
    path: str
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    diff: str = ""
    error: str | None = None
    review_grade: str = ""
    review_average: float = 0.0
    review_flagged: bool = False


@dataclass
class CorrectionRunReport:
    """Result of the blog correction agent (one record per file)."""

    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    input_dir: str = ""
    output_dir: str = ""
    files_scanned: int = 0
    max_searches: int = 0
    searches_used: int = 0
    changes: list[CorrectionChange] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    @property
    def files_changed(self) -> int:
        return sum(1 for change in self.changes if change.diff)

    @property
    def files_failed(self) -> int:
        return sum(1 for change in self.changes if change.error)

    @property
    def files_flagged(self) -> int:
        return sum(1 for change in self.changes if change.review_flagged)


def write_correction_report(report: CorrectionRunReport, reports_dir: str) -> tuple[str, str]:
    """Write the corrector's unified diff + JSON summary. Returns (json, diff)."""
    os.makedirs(reports_dir, exist_ok=True)
    stamp = _stamp(report.started_at)
    base = f"correct-{stamp}"

    diff_path = os.path.join(reports_dir, f"{base}.diff")
    diff_text = ""
    for change in report.changes:
        if change.diff:
            diff_text += change.diff
            if not change.diff.endswith("\n"):
                diff_text += "\n"
    _atomic_write_text(diff_path, diff_text)

    json_path = os.path.join(reports_dir, f"{base}.json")
    payload = {
        "agent": "correct_blogs",
        "started_at": report.started_at,
        "input_dir": report.input_dir,
        "output_dir": report.output_dir,
        "files_scanned": report.files_scanned,
        "files_changed": report.files_changed,
        "files_failed": report.files_failed,
        "files_flagged": report.files_flagged,
        "max_web_searches": report.max_searches,
        "search_calls": report.searches_used,
        "changes": [
            {
                "slug": change.slug,
                "path": change.path,
                "notes": change.notes,
                "warnings": change.warnings,
                "sources": change.sources,
                "has_changes": bool(change.diff),
                "error": change.error,
                "review_grade": change.review_grade,
                "review_average": change.review_average,
                "review_flagged": change.review_flagged,
            }
            for change in report.changes
        ],
    }
    _atomic_write_json(json_path, payload)
    return json_path, diff_path


@dataclass
class FrontmatterChange:
    """Per-file result of the frontmatter agent."""

    slug: str
    path: str
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    review_items: list[str] = field(default_factory=list)
    field_changes: list[dict] = field(default_factory=list)
    diff: str = ""
    error: str | None = None


@dataclass
class FrontmatterRunReport:
    """Result of the frontmatter validator/corrector (one record per file)."""

    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    input_dir: str = ""
    output_dir: str = ""
    schema: str = ""
    order_file: str = ""
    only_order: bool = False
    files_scanned: int = 0
    changes: list[FrontmatterChange] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    @property
    def files_changed(self) -> int:
        return sum(1 for change in self.changes if change.diff)

    @property
    def files_flagged(self) -> int:
        return sum(1 for change in self.changes if change.review_items)

    @property
    def files_failed(self) -> int:
        return sum(1 for change in self.changes if change.error)


def _render_frontmatter_changelog(report: FrontmatterRunReport) -> str:
    lines = [
        "# Frontmatter changelog",
        "",
        (
            f"Schema: {report.schema} | Files changed: {report.files_changed} | "
            f"Flagged for review: {report.files_flagged} | Generated: {report.started_at}"
        ),
        "",
    ]
    for change in report.changes:
        lines.append(f"## {change.slug}")
        lines.append("")
        if change.error:
            lines.append(f"- Error: {change.error}")
            lines.append("")
            lines.append("---")
            lines.append("")
            continue
        lines.append(f"- File: {change.path}")
        lines.append(f"- Changes made: {len(change.field_changes)}")
        lines.append(f"- Items requiring human review: {len(change.review_items)}")
        lines.append("")
        lines.append("### Frontmatter Changes")
        lines.append("")
        if change.field_changes:
            lines.append("| Field | Before | After | Reason |")
            lines.append("|-------|--------|-------|--------|")
            for entry in change.field_changes:
                before = str(entry.get("before", "")).replace("|", "\\|")
                after = str(entry.get("after", "")).replace("|", "\\|")
                reason = str(entry.get("reason", "")).replace("|", "\\|")
                lines.append(f"| {entry.get('field', '')} | {before} | {after} | {reason} |")
        else:
            lines.append("None.")
        lines.append("")
        if change.review_items:
            lines.append("### Items Requiring Human Review")
            lines.append("")
            lines.extend(f"- {item}" for item in change.review_items)
        if change.warnings:
            lines.append("")
            lines.append("### Warnings")
            lines.append("")
            lines.extend(f"- {warning}" for warning in change.warnings)
        lines.append("")
        lines.append("---")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def write_frontmatter_report(report: FrontmatterRunReport, reports_dir: str) -> tuple[str, str, str]:
    """Write the frontmatter diff, JSON summary and Markdown changelog."""
    os.makedirs(reports_dir, exist_ok=True)
    stamp = _stamp(report.started_at)
    base = f"frontmatter-{stamp}"

    diff_path = os.path.join(reports_dir, f"{base}.diff")
    diff_text = ""
    for change in report.changes:
        if change.diff:
            diff_text += change.diff
            if not change.diff.endswith("\n"):
                diff_text += "\n"
    _atomic_write_text(diff_path, diff_text)

    markdown_path = os.path.join(reports_dir, f"{base}.md")
    _atomic_write_text(markdown_path, _render_frontmatter_changelog(report))

    json_path = os.path.join(reports_dir, f"{base}.json")
    payload = {
        "agent": "fix_frontmatter",
        "started_at": report.started_at,
        "input_dir": report.input_dir,
        "output_dir": report.output_dir,
        "schema": report.schema,
        "order_file": report.order_file,
        "only_order": report.only_order,
        "files_scanned": report.files_scanned,
        "files_changed": report.files_changed,
        "files_flagged": report.files_flagged,
        "files_failed": report.files_failed,
        "changes": [
            {
                "slug": change.slug,
                "path": change.path,
                "notes": change.notes,
                "warnings": change.warnings,
                "review_items": change.review_items,
                "field_changes": change.field_changes,
                "has_changes": bool(change.diff),
                "error": change.error,
            }
            for change in report.changes
        ],
    }
    _atomic_write_json(json_path, payload)
    return json_path, diff_path, markdown_path


@dataclass
class FactCheckChange:
    """Per-file result of the fact checker."""

    slug: str
    path: str
    exams: list[str] = field(default_factory=list)
    findings: list[dict] = field(default_factory=list)
    review_items: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    diff: str = ""
    error: str | None = None

    @property
    def applied(self) -> int:
        return sum(1 for finding in self.findings if finding.get("applied"))

    @property
    def unresolved(self) -> int:
        return sum(1 for finding in self.findings if not finding.get("applied"))


@dataclass
class FactCheckRunReport:
    """Result of the fact checker (one record per file)."""

    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    input_dir: str = ""
    output_dir: str = ""
    facts_dir: str = ""
    files_scanned: int = 0
    changes: list[FactCheckChange] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    @property
    def files_with_exam(self) -> int:
        return sum(1 for change in self.changes if change.exams and not change.error)

    @property
    def files_changed(self) -> int:
        return sum(1 for change in self.changes if change.diff)

    @property
    def files_flagged(self) -> int:
        return sum(1 for change in self.changes if change.unresolved and not change.error)

    @property
    def files_failed(self) -> int:
        return sum(1 for change in self.changes if change.error)

    @property
    def total_applied(self) -> int:
        return sum(change.applied for change in self.changes)

    @property
    def total_unresolved(self) -> int:
        return sum(change.unresolved for change in self.changes)


def _render_factcheck_markdown(report: FactCheckRunReport) -> str:
    lines = [
        "# Fact check - human review",
        "",
        (
            f"Facts: {report.facts_dir} | Files scanned: {report.files_scanned} | "
            f"With exam: {report.files_with_exam} | Changed: {report.files_changed} | "
            f"Flagged: {report.files_flagged}"
        ),
        (
            f"Corrections applied: {report.total_applied} | "
            f"Unresolved (needs a human): {report.total_unresolved}"
        ),
        "",
    ]
    for change in report.changes:
        if change.error:
            lines.append(f"## {change.slug} - ERROR")
            lines.append("")
            lines.append(f"- {change.error}")
            lines.append("")
            lines.append("---")
            lines.append("")
            continue
        if not change.exams:
            continue
        lines.append(f"## {change.slug} [{', '.join(change.exams)}]")
        lines.append("")
        if change.findings:
            lines.append("| Category | Field | Find | Replace | Applied | Reason | Source |")
            lines.append("|----------|-------|------|---------|---------|--------|--------|")
            for finding in change.findings:
                find = str(finding.get("find", "")).replace("|", "\\|")
                replace = str(finding.get("replace", "")).replace("|", "\\|")
                reason = str(finding.get("reason", "")).replace("|", "\\|")
                source = str(finding.get("source", ""))
                applied = "yes" if finding.get("applied") else "no"
                lines.append(
                    f"| {finding.get('category', '')} | {finding.get('field', '')} | "
                    f"{find} | {replace} | {applied} | {reason} | {source} |"
                )
        else:
            lines.append("No discrepancies found.")
        if change.review_items:
            lines.append("")
            lines.append("**Needs human review:**")
            lines.extend(f"- {item}" for item in change.review_items)
        if change.warnings:
            lines.append("")
            lines.append("**Warnings:**")
            lines.extend(f"- {warning}" for warning in change.warnings)
        lines.append("")
        lines.append("---")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def write_factcheck_report(report: FactCheckRunReport, reports_dir: str) -> tuple[str, str, str]:
    """Write the fact-check diff, JSON summary and Markdown human-review report."""
    os.makedirs(reports_dir, exist_ok=True)
    stamp = _stamp(report.started_at)
    base = f"factcheck-{stamp}"

    diff_path = os.path.join(reports_dir, f"{base}.diff")
    diff_text = ""
    for change in report.changes:
        if change.diff:
            diff_text += change.diff
            if not change.diff.endswith("\n"):
                diff_text += "\n"
    _atomic_write_text(diff_path, diff_text)

    markdown_path = os.path.join(reports_dir, f"{base}.md")
    _atomic_write_text(markdown_path, _render_factcheck_markdown(report))

    json_path = os.path.join(reports_dir, f"{base}.json")
    payload = {
        "agent": "check_facts",
        "started_at": report.started_at,
        "input_dir": report.input_dir,
        "output_dir": report.output_dir,
        "facts_dir": report.facts_dir,
        "files_scanned": report.files_scanned,
        "files_with_exam": report.files_with_exam,
        "files_changed": report.files_changed,
        "files_flagged": report.files_flagged,
        "files_failed": report.files_failed,
        "corrections_applied": report.total_applied,
        "unresolved": report.total_unresolved,
        "changes": [
            {
                "slug": change.slug,
                "path": change.path,
                "exams": change.exams,
                "findings": change.findings,
                "review_items": change.review_items,
                "warnings": change.warnings,
                "sources": change.sources,
                "applied": change.applied,
                "unresolved": change.unresolved,
                "has_changes": bool(change.diff),
                "error": change.error,
            }
            for change in report.changes
        ],
    }
    _atomic_write_json(json_path, payload)
    return json_path, diff_path, markdown_path


def write_human_review(
    entries: list[dict],
    reports_dir: str,
    threshold: float,
    started_at: str = "",
) -> str:
    """Write a focused Markdown file listing flagged posts and what to fix.

    Each entry is a plain dict: ``slug``, ``grade``, ``average``, ``weaknesses``,
    ``factual_issues``, ``low_criteria``, ``changed``, ``warnings``, ``sources``.
    """
    os.makedirs(reports_dir, exist_ok=True)
    generated = started_at or datetime.now(UTC).isoformat()
    stamp = _stamp(generated)
    path = os.path.join(reports_dir, f"human-review-{stamp}.md")

    lines = [
        "# Human review - corrected posts below the target grade",
        "",
        f"Threshold: {threshold:.2f} | Flagged: {len(entries)} | Generated: {generated}",
        "",
    ]
    for entry in entries:
        lines.append(
            f"## {entry['slug']} - {entry.get('grade', '?')} ({entry.get('average', 0.0):.2f})"
        )
        lines.append("")
        lines.append("**Issues to fix:**")
        issues = list(entry.get("weaknesses") or [])
        issues += [f"[factual] {item}" for item in entry.get("factual_issues") or []]
        issues += [f"[low score] {item}" for item in entry.get("low_criteria") or []]
        if issues:
            lines.extend(f"- {issue}" for issue in issues)
        else:
            lines.append("- (no specific rubric feedback; review manually)")
        lines.append("")

        if entry.get("changed"):
            lines.append("**Already applied by the corrector:**")
            lines.extend(f"- {note}" for note in entry["changed"])
            lines.append("")
        if entry.get("warnings"):
            lines.append("**Corrector warnings:**")
            lines.extend(f"- {warning}" for warning in entry["warnings"])
            lines.append("")
        if entry.get("sources"):
            lines.append("**Sources:** " + ", ".join(entry["sources"]))
            lines.append("")
        lines.append("---")
        lines.append("")

    _atomic_write_text(path, "\n".join(lines).rstrip("\n") + "\n")
    return path

