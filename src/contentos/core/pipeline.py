"""Pipeline runner: wire loader -> agent -> diff report -> optional write."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from contentos.agents.corrector import (
    DEFAULT_CORRECTION_PROMPT,
    CorrectionSpec,
    generate_correction,
    load_correction_spec,
)
from contentos.agents.factchecker import (
    DEFAULT_FACTS_DIR,
    FactRegistry,
    generate_factcheck,
    load_fact_prompt,
    load_fact_registry,
)
from contentos.agents.frontmatter import (
    DEFAULT_EXAM_DB,
    DEFAULT_SEO_PROMPT,
    correct_frontmatter,
    load_exam_registry,
    load_frontmatter_schema,
    load_seo_spec,
)
from contentos.agents.interlink_reviewer import review_mapping, single_related_sources
from contentos.agents.interlinker import (
    DEFAULT_MAX_ATTEMPTS,
    finalize_links,
    frontmatter_exam_codes,
    generate_interlinks,
    source_slug,
)
from contentos.agents.link_stripper import strip_frontmatter_embeds, strip_links
from contentos.agents.rater import (
    DEFAULT_PROMPT_PATH,
    Rubric,
    build_markdown,
    generate_rating,
    load_rubric,
    result_to_dict,
)
from contentos.core import reporting
from contentos.core.inventory import InventoryItem, load_inventory
from contentos.core.llm import (
    FallbackLLM,
    FatalLLMError,
    RateLimitedLLM,
    RateLimiter,
    SearchBudget,
    deepseek_available,
    deepseek_llm,
    gemini_llm,
)
from contentos.core.loaders import ContentDocument, discover_documents, load_document, parse_document

DEFAULT_FRONTMATTER_PROMPT = os.path.join("prompts", "examos-blog-frontmatter.md")
DEFAULT_FACT_PROMPT = os.path.join("prompts", "examos-content-facts.md")


@dataclass
class SourceOutcome:
    """Result of processing one source, suitable for concurrent collection."""

    slug: str
    path: str
    analysis: object | None = None
    links: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class RatingOutcome:
    """Result of rating one document, suitable for concurrent collection."""

    slug: str
    path: str
    result: object | None = None
    error: str | None = None


@dataclass
class CorrectionOutcome:
    """Result of correcting one document, suitable for concurrent collection."""

    slug: str
    path: str
    result: object | None = None
    before: str = ""
    error: str | None = None


@dataclass
class FrontmatterOutcome:
    """Result of validating one document's frontmatter."""

    slug: str
    path: str
    result: object | None = None
    before: str = ""
    error: str | None = None


ProgressCallback = Callable[[int, int, SourceOutcome], None]
StartCallback = Callable[[int, int, str], None]
RetryCallback = Callable[[int, int, str, int, int, float, Exception], None]
ProviderSwitchCallback = Callable[[str, Exception], None]

# Grounded (Google Search) calls are much slower than plain ones, so they get a longer
# default timeout than the per-request ``--timeout`` used for ungrounded calls.
DEFAULT_SEARCH_TIMEOUT = 300.0


def run_link_strip(
    input_dir: str,
    output_dir: str | None = None,
    reports_dir: str = "reports",
    apply: bool = False,
) -> reporting.RunReport:
    """Run the link-strip agent over a directory of content files.

    If apply=True, cleaned files are written to output_dir (input_dir is never
    modified). Reports are always written to reports_dir.
    """
    files = discover_documents(input_dir)
    report = reporting.RunReport(agent="strip_links")
    report.files_scanned = len(files)

    for path in files:
        doc = load_document(path)
        new_body, notes = strip_links(doc)
        # Roadmap embeds live in nested YAML, so also drop bare embed lines there.
        new_frontmatter, fm_notes = strip_frontmatter_embeds(doc.frontmatter)

        change = reporting.FileChange(path=path, notes=fm_notes + notes)
        rel = os.path.relpath(path, input_dir)

        rendered = ContentDocument(
            path=path,
            frontmatter=new_frontmatter,
            body=new_body,
            has_frontmatter=doc.has_frontmatter,
        )
        before = doc.render()
        after = rendered.render()

        if before != after:
            change.modified_lines = sum(1 for a, b in zip(before.splitlines(), after.splitlines()) if a != b)
            change.removed_lines = max(0, len(before.splitlines()) - len(after.splitlines()))
            change.diff = reporting.make_diff(rel, before, after)

        report.add(change)

        if apply and output_dir and before != after:
            dest = os.path.join(output_dir, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "w", encoding="utf-8") as f:
                f.write(after)

    json_path, diff_path = reporting.write_report(report, reports_dir)
    report.extra = {"json_report": json_path, "diff_report": diff_path}
    return report


def _process_source(
    path: str,
    index: int,
    total: int,
    inventory: list[InventoryItem],
    llm,
    max_attempts: int,
    on_start: StartCallback | None = None,
    on_retry: RetryCallback | None = None,
) -> SourceOutcome:
    """Analyse one source and run the guardrails. Never raises."""
    doc = load_document(path)
    current_slug = source_slug(doc)
    if on_start:
        on_start(index, total, current_slug)

    def _report_retry(attempt: int, attempts: int, wait: float, exc: Exception) -> None:
        if on_retry:
            on_retry(index, total, current_slug, attempt, attempts, wait, exc)

    try:
        analysis = generate_interlinks(
            doc, current_slug, inventory, llm, max_attempts=max_attempts, on_retry=_report_retry
        )
        links, warnings = finalize_links(
            analysis.links,
            source_slug=current_slug,
            primary_exam=analysis.primary_exam,
            inventory=inventory,
            exam_codes=frontmatter_exam_codes(doc.frontmatter),
        )
    except FatalLLMError:
        raise
    except Exception as exc:  # noqa: BLE001 - surface any per-file failure in the report
        return SourceOutcome(slug=current_slug, path=path, error=f"{type(exc).__name__}: {exc}")
    return SourceOutcome(slug=current_slug, path=path, analysis=analysis, links=links, warnings=warnings)


def _run_concurrently(
    files,
    inventory,
    llm,
    workers: int,
    max_attempts: int,
    on_progress: ProgressCallback | None = None,
    on_start: StartCallback | None = None,
    on_retry: RetryCallback | None = None,
) -> list[SourceOutcome]:
    outcomes: list[SourceOutcome | None] = [None] * len(files)
    total = len(files)
    completed = 0
    executor = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = {
            executor.submit(
                _process_source, path, index, total, inventory, llm, max_attempts, on_start, on_retry
            ): index
            for index, path in enumerate(files, start=1)
        }
        for future in as_completed(futures):
            outcome = future.result()
            outcomes[futures[future] - 1] = outcome
            completed += 1
            if on_progress:
                on_progress(completed, total, outcome)
    except BaseException:
        # Do not block on in-flight LLM calls when the user interrupts.
        executor.shutdown(wait=False, cancel_futures=True)
        raise
    else:
        executor.shutdown(wait=True)
    return [outcome for outcome in outcomes if outcome is not None]


def _rate_source(
    path: str,
    index: int,
    total: int,
    llm,
    rubric: Rubric,
    max_attempts: int,
    on_start: StartCallback | None = None,
    on_retry: RetryCallback | None = None,
    search_llm=None,
    search_budget: SearchBudget | None = None,
    force_ground: bool = False,
) -> RatingOutcome:
    """Rate one document against the rubric. Never raises."""
    doc = load_document(path)
    current_slug = source_slug(doc)
    if on_start:
        on_start(index, total, current_slug)

    def _report_retry(attempt: int, attempts: int, wait: float, exc: Exception) -> None:
        if on_retry:
            on_retry(index, total, current_slug, attempt, attempts, wait, exc)

    try:
        result = generate_rating(
            doc,
            current_slug,
            llm,
            rubric,
            max_attempts=max_attempts,
            on_retry=_report_retry,
            search_llm=search_llm,
            force_ground=force_ground,
            search_budget=search_budget,
        )
    except FatalLLMError:
        raise
    except Exception as exc:  # noqa: BLE001 - surface any per-file failure in the report
        return RatingOutcome(slug=current_slug, path=path, error=f"{type(exc).__name__}: {exc}")
    return RatingOutcome(slug=current_slug, path=path, result=result)


def _run_ratings_concurrently(
    files,
    llm,
    rubric: Rubric,
    workers: int,
    max_attempts: int,
    on_progress: ProgressCallback | None = None,
    on_start: StartCallback | None = None,
    on_retry: RetryCallback | None = None,
    search_llm=None,
    search_budget: SearchBudget | None = None,
    force_ground: bool = False,
) -> list[RatingOutcome]:
    outcomes: list[RatingOutcome | None] = [None] * len(files)
    total = len(files)
    completed = 0
    executor = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = {
            executor.submit(
                _rate_source,
                path,
                index,
                total,
                llm,
                rubric,
                max_attempts,
                on_start,
                on_retry,
                search_llm,
                search_budget,
                force_ground,
            ): index
            for index, path in enumerate(files, start=1)
        }
        for future in as_completed(futures):
            outcome = future.result()
            outcomes[futures[future] - 1] = outcome
            completed += 1
            if on_progress:
                on_progress(completed, total, outcome)
    except BaseException:
        executor.shutdown(wait=False, cancel_futures=True)
        raise
    else:
        executor.shutdown(wait=True)
    return [outcome for outcome in outcomes if outcome is not None]


def _build_llm(
    rate_limit: float,
    timeout: float | None,
    fallback: bool,
    on_provider_switch: ProviderSwitchCallback | None = None,
    *,
    enable_search: bool = False,
    limiter: RateLimiter | None = None,
    search_timeout: float | None = None,
):
    """Gemini model (rate limited), optionally grounded, wrapped with a DeepSeek fallback.

    A grounded (Google Search) call is much slower than a plain one, so ``search_timeout``
    overrides ``timeout`` for it (a short timeout would spuriously fall back to DeepSeek,
    which cannot search).
    """
    limiter = limiter or RateLimiter(min_interval=(1.0 / rate_limit) if rate_limit > 0 else 0.0)
    effective_timeout = search_timeout if (enable_search and search_timeout) else timeout
    primary = RateLimitedLLM(
        gemini_llm(timeout=effective_timeout, enable_search=enable_search), limiter
    )
    if fallback and deepseek_available():
        secondary = deepseek_llm(timeout=timeout)
        return FallbackLLM(
            primary,
            secondary,
            on_switch=(
                (lambda exc: on_provider_switch("deepseek", exc)) if on_provider_switch else None
            ),
        )
    return primary


def run_interlink(
    input_dir: str,
    inventory_path: str,
    output_path: str = "output/interlinks.json",
    reports_dir: str = "reports",
    apply: bool = False,
    limit: int | None = None,
    slug: str | None = None,
    workers: int = 4,
    rate_limit: float = 0.0,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    timeout: float | None = 120.0,
    fallback: bool = False,
    on_progress: ProgressCallback | None = None,
    on_start: StartCallback | None = None,
    on_retry: RetryCallback | None = None,
    on_provider_switch: ProviderSwitchCallback | None = None,
) -> reporting.InterlinkRunReport:
    """Run the interlink agent over content files and build a link mapping.

    Sources are analysed concurrently (``workers`` threads) with an optional
    client-side rate limit (``rate_limit`` requests per second), per-call
    retries (``max_attempts``) and ``timeout`` (seconds per request).
    ``on_start``, ``on_retry`` and ``on_progress`` stream live status.

    When ``fallback`` is set and DEEPSEEK_API_KEY is configured, a Gemini call
    that times out is retried against the DeepSeek API for that call only (the
    fallback is not sticky; ``on_provider_switch`` reports each fallback). The
    mapping is always reported; it is only written to output_path when apply=True.
    """
    output_path = _resolve_output_path(output_path)

    files = discover_documents(input_dir)
    if slug:
        files = [
            path
            for path in files
            if os.path.splitext(os.path.basename(path))[0] == slug
            or source_slug(load_document(path)) == slug
        ]
    if limit is not None:
        files = files[:limit]

    inventory = load_inventory(inventory_path)
    llm = _build_llm(rate_limit, timeout, fallback, on_provider_switch)

    report = reporting.InterlinkRunReport()
    report.files_scanned = len(files)

    workers = max(1, workers)
    if workers == 1:
        outcomes = []
        for index, path in enumerate(files, start=1):
            outcome = _process_source(
                path, index, len(files), inventory, llm, max_attempts, on_start, on_retry
            )
            outcomes.append(outcome)
            if on_progress:
                on_progress(index, len(files), outcome)
    else:
        outcomes = _run_concurrently(
            files, inventory, llm, workers, max_attempts, on_progress, on_start, on_retry
        )

    mapping: dict[str, list[dict]] = {}
    for outcome in outcomes:
        if outcome.error:
            report.sources.append(
                reporting.SourceInterlink(slug=outcome.slug, path=outcome.path, error=outcome.error)
            )
            continue
        analysis = outcome.analysis
        mapping[outcome.slug] = outcome.links
        report.sources.append(
            reporting.SourceInterlink(
                slug=outcome.slug,
                path=outcome.path,
                primary_exam=getattr(analysis, "primary_exam", None),
                primary_topic=getattr(analysis, "primary_topic", ""),
                audience=getattr(analysis, "audience", ""),
                no_exam_found=getattr(analysis, "no_exam_found", False),
                links=outcome.links,
                warnings=outcome.warnings,
            )
        )

    json_report = reporting.write_interlink_report(report, reports_dir)
    report.extra = {
        "json_report": json_report,
        "output": output_path if apply else None,
        "sources": len(mapping),
    }

    if apply and mapping:
        _merge_mapping(mapping, output_path)

    return report


def _resolve_output_path(output_path: str) -> str:
    """Treat a directory as the place to write ``interlinks.json``.

    Guards against the easy mistake of passing ``--output output`` (a folder)
    where a file path is expected.
    """
    if not output_path:
        return output_path
    if os.path.isdir(output_path) or output_path.endswith(("/", "\\")):
        return os.path.join(output_path, "interlinks.json")
    return output_path


def _merge_mapping(mapping: dict[str, list[dict]], output_path: str) -> None:
    """Merge the generated mapping into output_path, preserving unrelated keys."""
    existing: dict = {}
    if os.path.exists(output_path):
        try:
            with open(output_path, encoding="utf-8") as f:
                existing = json.load(f)
        except (json.JSONDecodeError, OSError):
            existing = {}
    existing.update(mapping)

    parent = os.path.dirname(output_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _load_mapping(path: str) -> dict[str, list[dict]]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise TypeError(f"Interlink mapping must be a JSON object: {path}")
    return data


def _write_mapping(mapping: dict[str, list[dict]], path: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(mapping, f, indent=2, ensure_ascii=False)
        f.write("\n")


def run_review_interlinks(
    mapping_path: str = "output/interlinks.json",
    input_dir: str = "blogs",
    inventory_path: str = "input/content_inventory.json",
    output_path: str | None = None,
    reports_dir: str = "reports",
    apply: bool = False,
    redo: bool = True,
    single_related: int = 1,
    workers: int = 4,
    rate_limit: float = 0.0,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    timeout: float | None = 120.0,
    fallback: bool = False,
    limit: int | None = None,
    slug: str | None = None,
    on_progress: ProgressCallback | None = None,
    on_start: StartCallback | None = None,
    on_retry: RetryCallback | None = None,
    on_provider_switch: ProviderSwitchCallback | None = None,
) -> reporting.ReviewRunReport:
    """Repair an existing interlinks mapping and optionally redo thin sources.

    Deterministic repairs (reciprocal inline links, 100% -> related, misplaced
    quizzes) always run. When ``redo`` is set, sources whose Related Reading
    section holds exactly ``single_related`` item are re-analysed with the LLM
    and only their entry in the mapping is replaced. Nothing is written unless
    ``apply`` is set; a report is always written to ``reports_dir``.
    """
    mapping = _load_mapping(mapping_path)
    destination = _resolve_output_path(output_path) if output_path else mapping_path
    report = reporting.ReviewRunReport(mapping_path=mapping_path, output_path=destination)
    report.sources_scanned = len(mapping)

    inventory = load_inventory(inventory_path)

    redo_slugs = single_related_sources(mapping, single_related) if redo else []
    if slug:
        redo_slugs = [candidate for candidate in redo_slugs if candidate == slug]
    if limit is not None:
        redo_slugs = redo_slugs[:limit]
    report.redo_sources = list(redo_slugs)

    mapping, notes = review_mapping(mapping, inventory)
    report.incomplete_fixed = notes["incomplete"]
    report.anchors_fixed = notes["anchors"]
    report.reciprocal_dropped = notes["reciprocal"]
    report.quizzes_fixed = notes["quiz"]
    report.promoted_to_related = notes["promoted"]

    if redo_slugs:
        by_slug = {source_slug(load_document(path)): path for path in discover_documents(input_dir)}
        targets = [(candidate, by_slug[candidate]) for candidate in redo_slugs if candidate in by_slug]
        for missing in (candidate for candidate in redo_slugs if candidate not in by_slug):
            report.redo_results.append(
                reporting.SourceInterlink(
                    slug=missing, path="", error="source document not found"
                )
            )
        if targets:
            llm = _build_llm(rate_limit, timeout, fallback, on_provider_switch)
            if workers == 1:
                outcomes = [
                    _process_source(
                        path, index, len(targets), inventory, llm, max_attempts, on_start, on_retry
                    )
                    for index, (_slug, path) in enumerate(targets, start=1)
                ]
                if on_progress:
                    for index, outcome in enumerate(outcomes, start=1):
                        on_progress(index, len(targets), outcome)
            else:
                outcomes = _run_concurrently(
                    [path for _slug, path in targets],
                    inventory,
                    llm,
                    max(1, workers),
                    max_attempts,
                    on_progress,
                    on_start,
                    on_retry,
                )
            for outcome in outcomes:
                report.redo_results.append(
                    reporting.SourceInterlink(
                        slug=outcome.slug,
                        path=outcome.path,
                        primary_exam=getattr(outcome.analysis, "primary_exam", None),
                        links=outcome.links,
                        warnings=outcome.warnings,
                        error=outcome.error,
                    )
                )
                if not outcome.error:
                    mapping[outcome.slug] = outcome.links
            mapping, notes = review_mapping(mapping, inventory)
            report.incomplete_fixed.extend(notes["incomplete"])
            report.anchors_fixed.extend(notes["anchors"])
            report.reciprocal_dropped.extend(notes["reciprocal"])
            report.quizzes_fixed.extend(notes["quiz"])
            report.promoted_to_related.extend(notes["promoted"])

    json_report = reporting.write_review_report(report, reports_dir)
    report.extra = {"json_report": json_report, "output": destination if apply else None}
    if apply:
        _write_mapping(mapping, destination)
    return report


def run_rate_blogs(
    input_dir: str,
    reports_dir: str = "reports",
    prompt_path: str = DEFAULT_PROMPT_PATH,
    limit: int | None = None,
    slug: str | None = None,
    workers: int = 4,
    rate_limit: float = 0.0,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    timeout: float | None = 120.0,
    search_timeout: float | None = DEFAULT_SEARCH_TIMEOUT,
    fallback: bool = False,
    verify_facts: bool = True,
    force_ground: bool = False,
    max_searches: int = 1500,
    on_progress: ProgressCallback | None = None,
    on_start: StartCallback | None = None,
    on_retry: RetryCallback | None = None,
    on_provider_switch: ProviderSwitchCallback | None = None,
) -> reporting.RatingRunReport:
    """Rate every document under ``input_dir`` against the rubric prompt.

    The rubric (criteria, weights, grade bands, prompt text) is loaded from
    ``prompt_path``. Documents are analysed concurrently (``workers`` threads)
    with optional rate limiting, per-call retries and a DeepSeek fallback.

    When ``verify_facts`` is set, a document whose first pass flags any
    ``[UNVERIFIABLE]`` claim is re-rated with Gemini's Google Search grounding
    enabled, so the limited search quota is only spent where it matters. The
    shared ``max_searches`` budget (<= 0 means unlimited) caps grounded calls for
    the whole run. Always writes a combined markdown report and a JSON summary;
    content is never modified.
    """
    rubric = load_rubric(prompt_path)

    files = discover_documents(input_dir)
    if slug:
        files = [
            path
            for path in files
            if os.path.splitext(os.path.basename(path))[0] == slug
            or source_slug(load_document(path)) == slug
        ]
    if limit is not None:
        files = files[:limit]

    report = reporting.RatingRunReport(input_dir=input_dir, rubric=rubric.name)
    report.files_scanned = len(files)
    report.max_searches = max_searches

    if not files:
        json_report, markdown_report = reporting.write_rating_report(report, reports_dir)
        report.extra = {"json_report": json_report, "markdown_report": markdown_report}
        return report

    limiter = RateLimiter(min_interval=(1.0 / rate_limit) if rate_limit > 0 else 0.0)
    llm = _build_llm(rate_limit, timeout, fallback, on_provider_switch, limiter=limiter)
    search_llm = (
        _build_llm(
            rate_limit,
            timeout,
            fallback,
            on_provider_switch,
            enable_search=True,
            limiter=limiter,
            search_timeout=search_timeout,
        )
        if verify_facts
        else None
    )
    search_budget = SearchBudget(max_searches) if search_llm is not None else None

    workers = max(1, workers)
    results = []

    def _record(index: int, count: int, outcome) -> None:
        """Accumulate one outcome and flush the report so far.

        Writing after every completed file (same timestamped paths, overwritten)
        means an interrupted or crashed run still leaves the ratings finished so
        far on disk.
        """
        if outcome.error:
            report.errors.append(
                {"slug": outcome.slug, "path": outcome.path, "error": outcome.error}
            )
        else:
            results.append(outcome.result)
            report.ratings.append(result_to_dict(outcome.result))
        if search_budget is not None:
            report.searches_used = search_budget.used
        report.markdown = build_markdown(results)
        try:
            reporting.write_rating_report(report, reports_dir)
        except OSError:
            pass  # a partial-write failure must not abort the run
        if on_progress:
            on_progress(index, count, outcome)

    if workers == 1:
        for index, path in enumerate(files, start=1):
            outcome = _rate_source(
                path,
                index,
                len(files),
                llm,
                rubric,
                max_attempts,
                on_start,
                on_retry,
                search_llm,
                search_budget,
                force_ground,
            )
            _record(index, len(files), outcome)
    else:
        _run_ratings_concurrently(
            files,
            llm,
            rubric,
            workers,
            max_attempts,
            _record,
            on_start,
            on_retry,
            search_llm,
            search_budget,
            force_ground,
        )

    report.markdown = build_markdown(results)
    json_report, markdown_report = reporting.write_rating_report(report, reports_dir)
    report.extra = {"json_report": json_report, "markdown_report": markdown_report}
    return report


def _correct_source(
    path: str,
    index: int,
    total: int,
    spec: CorrectionSpec,
    llm,
    search_llm,
    verify_faq: bool,
    fix_facts: bool,
    fix_grammar: bool,
    fix_slop: bool,
    fix_examos: bool,
    seo: bool,
    deslop_slugs: set[str] | None,
    review: bool,
    review_threshold: float,
    rating_rubric: Rubric | None,
    max_attempts: int,
    on_start: StartCallback | None = None,
    on_retry: RetryCallback | None = None,
    search_budget: SearchBudget | None = None,
    faq_applies: bool = True,
) -> CorrectionOutcome:
    """Correct one document. Never raises (except fatal LLM misconfiguration)."""
    doc = load_document(path)
    current_slug = source_slug(doc)
    if on_start:
        on_start(index, total, current_slug)

    def _report_retry(attempt: int, attempts: int, wait: float, exc: Exception) -> None:
        if on_retry:
            on_retry(index, total, current_slug, attempt, attempts, wait, exc)

    # When a ratings set is given, only de-slop posts whose AI-slop score flagged them.
    run_slop = fix_slop and (deslop_slugs is None or current_slug in deslop_slugs)

    try:
        result = generate_correction(
            doc,
            current_slug,
            spec,
            llm,
            search_llm=search_llm,
            verify_faq=verify_faq,
            fix_facts=fix_facts,
            fix_grammar=fix_grammar,
            fix_slop=run_slop,
            fix_examos=fix_examos,
            seo=seo,
            faq_applies=faq_applies,
            max_attempts=max_attempts,
            on_retry=_report_retry,
            search_budget=search_budget,
        )
        if review and rating_rubric is not None and result.notes:
            _review_corrected(
                result,
                doc,
                current_slug,
                llm,
                search_llm,
                rating_rubric,
                review_threshold,
                max_attempts,
                _report_retry,
                search_budget,
            )
    except FatalLLMError:
        raise
    except Exception as exc:  # noqa: BLE001 - surface any per-file failure in the report
        return CorrectionOutcome(slug=current_slug, path=path, error=f"{type(exc).__name__}: {exc}")
    return CorrectionOutcome(
        slug=current_slug, path=path, result=result, before=doc.render()
    )


def _review_corrected(
    result,
    doc,
    slug: str,
    llm,
    search_llm,
    rubric: Rubric,
    threshold: float,
    max_attempts: int,
    on_retry,
    search_budget: SearchBudget | None,
) -> None:
    """Re-rate the corrected text and flag posts that did not reach the threshold."""
    reviewed = parse_document(result.new_text, doc.path)
    try:
        rating = generate_rating(
            reviewed,
            slug,
            llm,
            rubric,
            max_attempts=max_attempts,
            on_retry=on_retry,
            search_llm=search_llm,
            verify_facts=search_llm is not None,
            search_budget=search_budget,
        )
    except Exception as err:  # noqa: BLE001 - a failed review must not fail the correction
        result.warnings.append(f"post-correction review failed: {type(err).__name__}: {err}")
        return
    result.review_grade = rating.grade
    result.review_average = rating.average
    result.review_result = rating
    if rating.average < threshold:
        result.review_flagged = True
        result.warnings.append(
            f"post-correction rating {rating.grade} ({rating.average:.2f}) is below the A threshold"
        )


def _run_corrections_concurrently(
    files,
    spec: CorrectionSpec,
    llm,
    search_llm,
    verify_faq: bool,
    fix_facts: bool,
    fix_grammar: bool,
    fix_slop: bool,
    fix_examos: bool,
    seo: bool,
    deslop_slugs: set[str] | None,
    review: bool,
    review_threshold: float,
    rating_rubric: Rubric | None,
    workers: int,
    max_attempts: int,
    on_progress: ProgressCallback | None = None,
    on_start: StartCallback | None = None,
    on_retry: RetryCallback | None = None,
    search_budget: SearchBudget | None = None,
    faq_applies: bool = True,
) -> list[CorrectionOutcome]:
    outcomes: list[CorrectionOutcome | None] = [None] * len(files)
    total = len(files)
    completed = 0
    executor = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = {
            executor.submit(
                _correct_source,
                path,
                index,
                total,
                spec,
                llm,
                search_llm,
                verify_faq,
                fix_facts,
                fix_grammar,
                fix_slop,
                fix_examos,
                seo,
                deslop_slugs,
                review,
                review_threshold,
                rating_rubric,
                max_attempts,
                on_start,
                on_retry,
                search_budget,
                faq_applies,
            ): index
            for index, path in enumerate(files, start=1)
        }
        for future in as_completed(futures):
            outcome = future.result()
            outcomes[futures[future] - 1] = outcome
            completed += 1
            if on_progress:
                on_progress(completed, total, outcome)
    except BaseException:
        executor.shutdown(wait=False, cancel_futures=True)
        raise
    else:
        executor.shutdown(wait=True)
    return [outcome for outcome in outcomes if outcome is not None]


def _write_corrected(path: str, input_dir: str, output_dir: str, text: str) -> None:
    rel = os.path.relpath(path, input_dir)
    dest = os.path.join(output_dir, rel)
    parent = os.path.dirname(dest)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(dest, "w", encoding="utf-8") as f:
        f.write(text)


def run_correct_blogs(
    input_dir: str,
    output_dir: str | None = None,
    reports_dir: str = "reports",
    prompt_path: str = DEFAULT_CORRECTION_PROMPT,
    apply: bool = False,
    limit: int | None = None,
    slug: str | None = None,
    workers: int = 4,
    rate_limit: float = 0.0,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    timeout: float | None = 120.0,
    search_timeout: float | None = DEFAULT_SEARCH_TIMEOUT,
    fallback: bool = False,
    verify_faq: bool = True,
    fix_facts: bool = True,
    fix_grammar: bool = True,
    fix_slop: bool = True,
    fix_examos: bool = True,
    seo: bool = False,
    deslop_slugs: set[str] | None = None,
    slug_filter: set[str] | None = None,
    review: bool = True,
    review_threshold: float = 4.3,
    rating_prompt: str = DEFAULT_PROMPT_PATH,
    faq_applies: bool = True,
    max_searches: int = 1500,
    on_progress: ProgressCallback | None = None,
    on_start: StartCallback | None = None,
    on_retry: RetryCallback | None = None,
    on_provider_switch: ProviderSwitchCallback | None = None,
) -> reporting.CorrectionRunReport:
    """Correct content files and report the changes.

    Deterministic fixes (FAQ heading, dangling endings) always run; a missing/short FAQ
    section is regenerated with Google Search grounding. SEO metadata is **off by
    default** (``fix-frontmatter`` owns title/seoTitle/seoDescription); pass ``seo=True``
    to enable the corrector's own SEO pass.

    When ``review`` is set, every corrected file is re-rated with the rating rubric
    (``rating_prompt``) and flagged when its average is below ``review_threshold``
    (default 4.3, the A band), so the report surfaces posts for human review. Files
    are only written when ``apply`` is set, and then under ``output_dir`` (the input
    directory is never modified).
    """
    spec = load_correction_spec(prompt_path)
    rating_rubric = load_rubric(rating_prompt) if review else None

    files = discover_documents(input_dir)
    if slug:
        files = [
            path
            for path in files
            if os.path.splitext(os.path.basename(path))[0] == slug
            or source_slug(load_document(path)) == slug
        ]
    if slug_filter is not None:
        files = [path for path in files if source_slug(load_document(path)) in slug_filter]
    if limit is not None:
        files = files[:limit]

    report = reporting.CorrectionRunReport(input_dir=input_dir, output_dir=output_dir or "")
    report.files_scanned = len(files)
    report.max_searches = max_searches

    if not files:
        json_report, diff_report = reporting.write_correction_report(report, reports_dir)
        report.extra = {"json_report": json_report, "diff_report": diff_report}
        return report

    limiter = RateLimiter(min_interval=(1.0 / rate_limit) if rate_limit > 0 else 0.0)
    llm = _build_llm(rate_limit, timeout, fallback, on_provider_switch, limiter=limiter)
    search_llm = (
        _build_llm(
            rate_limit,
            timeout,
            fallback,
            on_provider_switch,
            enable_search=True,
            limiter=limiter,
            search_timeout=search_timeout,
        )
        if (verify_faq or review)
        else None
    )
    search_budget = SearchBudget(max_searches) if search_llm is not None else None

    workers = max(1, workers)
    reviewed: list[tuple] = []  # (CorrectionResult, RatingResult) for reviewed files

    def _record(index: int, count: int, outcome: CorrectionOutcome) -> None:
        if outcome.error:
            change = reporting.CorrectionChange(
                slug=outcome.slug, path=outcome.path, error=outcome.error
            )
        else:
            result = outcome.result
            rel = os.path.relpath(outcome.path, input_dir)
            after = result.new_text
            diff = reporting.make_diff(rel, outcome.before, after) if outcome.before != after else ""
            change = reporting.CorrectionChange(
                slug=outcome.slug,
                path=outcome.path,
                notes=result.notes,
                warnings=result.warnings,
                sources=result.sources,
                diff=diff,
                review_grade=result.review_grade,
                review_average=result.review_average,
                review_flagged=result.review_flagged,
            )
            if getattr(result, "review_result", None) is not None:
                reviewed.append((result, result.review_result))
            if apply and output_dir and diff:
                _write_corrected(outcome.path, input_dir, output_dir, after)
        report.changes.append(change)
        if search_budget is not None:
            report.searches_used = search_budget.used
        try:
            reporting.write_correction_report(report, reports_dir)
        except OSError:
            pass  # a partial-write failure must not abort the run
        if on_progress:
            on_progress(index, count, outcome)

    if workers == 1:
        for index, path in enumerate(files, start=1):
            outcome = _correct_source(
                path,
                index,
                len(files),
                spec,
                llm,
                search_llm,
                verify_faq,
                fix_facts,
                fix_grammar,
                fix_slop,
                fix_examos,
                seo,
                deslop_slugs,
                review,
                review_threshold,
                rating_rubric,
                max_attempts,
                on_start,
                on_retry,
                search_budget,
                faq_applies,
            )
            _record(index, len(files), outcome)
    else:
        _run_corrections_concurrently(
            files,
            spec,
            llm,
            search_llm,
            verify_faq,
            fix_facts,
            fix_grammar,
            fix_slop,
            fix_examos,
            seo,
            deslop_slugs,
            review,
            review_threshold,
            rating_rubric,
            workers,
            max_attempts,
            _record,
            on_start,
            on_retry,
            search_budget,
            faq_applies,
        )

    json_report, diff_report = reporting.write_correction_report(report, reports_dir)
    report.extra = {"json_report": json_report, "diff_report": diff_report}

    if reviewed:
        rating_report = reporting.RatingRunReport(
            input_dir=output_dir or input_dir,
            rubric=rating_rubric.name if rating_rubric else "",
            max_searches=max_searches,
            searches_used=search_budget.used if search_budget else 0,
        )
        rating_report.files_scanned = len(reviewed)
        rating_report.ratings = [result_to_dict(rating) for _result, rating in reviewed]
        rating_report.markdown = build_markdown([rating for _result, rating in reviewed])
        rating_json, rating_md = reporting.write_rating_report(rating_report, reports_dir)
        report.extra["rating_report"] = rating_md
        report.extra["rating_json"] = rating_json

        flagged = [
            _human_review_entry(result, rating, review_threshold)
            for result, rating in reviewed
            if result.review_flagged
        ]
        if flagged:
            report.extra["human_review_report"] = reporting.write_human_review(
                flagged, reports_dir, review_threshold, report.started_at
            )

    return report


def _load_order_file(path: str) -> dict[str, int]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise TypeError(f"Order file must be a JSON object of slug -> integer: {path}")
    order: dict[str, int] = {}
    for key, value in data.items():
        try:
            order[str(key)] = int(value)
        except (TypeError, ValueError):
            continue
    return order


def _fix_source(
    path: str,
    index: int,
    total: int,
    schema,
    order_map: dict[str, int] | None,
    only_order: bool,
    llm,
    max_attempts: int,
    exam_registry,
    seo_llm=None,
    seo_spec=None,
    nested_preserve: tuple[str, ...] = (),
    on_start: StartCallback | None = None,
    on_retry: RetryCallback | None = None,
) -> FrontmatterOutcome:
    """Validate one document's frontmatter. Never raises."""
    doc = load_document(path)
    current_slug = source_slug(doc)
    if on_start:
        on_start(index, total, current_slug)

    def _report_retry(attempt: int, attempts: int, wait: float, exc: Exception) -> None:
        if on_retry:
            on_retry(index, total, current_slug, attempt, attempts, wait, exc)

    try:
        result = correct_frontmatter(
            doc,
            schema,
            order_map=order_map,
            only_order=only_order,
            llm=llm,
            max_attempts=max_attempts,
            on_retry=_report_retry,
            exam_registry=exam_registry,
            seo_llm=seo_llm,
            seo_spec=seo_spec,
            nested_preserve=nested_preserve,
        )
    except FatalLLMError:
        raise
    except Exception as exc:  # noqa: BLE001 - surface any per-file failure in the report
        return FrontmatterOutcome(slug=current_slug, path=path, error=f"{type(exc).__name__}: {exc}")
    return FrontmatterOutcome(slug=current_slug, path=path, result=result, before=doc.render())


def _run_frontmatter_concurrently(
    files,
    schema,
    order_map,
    only_order: bool,
    llm,
    workers: int,
    max_attempts: int,
    exam_registry,
    seo_llm=None,
    seo_spec=None,
    nested_preserve: tuple[str, ...] = (),
    on_progress: ProgressCallback | None = None,
    on_start: StartCallback | None = None,
    on_retry: RetryCallback | None = None,
) -> list[FrontmatterOutcome]:
    outcomes: list[FrontmatterOutcome | None] = [None] * len(files)
    total = len(files)
    completed = 0
    executor = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = {
            executor.submit(
                _fix_source,
                path,
                index,
                total,
                schema,
                order_map,
                only_order,
                llm,
                max_attempts,
                exam_registry,
                seo_llm,
                seo_spec,
                nested_preserve,
                on_start,
                on_retry,
            ): index
            for index, path in enumerate(files, start=1)
        }
        for future in as_completed(futures):
            outcome = future.result()
            outcomes[futures[future] - 1] = outcome
            completed += 1
            if on_progress:
                on_progress(completed, total, outcome)
    except BaseException:
        executor.shutdown(wait=False, cancel_futures=True)
        raise
    else:
        executor.shutdown(wait=True)
    return [outcome for outcome in outcomes if outcome is not None]


def run_fix_frontmatter(
    input_dir: str,
    output_dir: str | None = "output_frontmatter",
    reports_dir: str = "reports",
    prompt_path: str = DEFAULT_FRONTMATTER_PROMPT,
    apply: bool = False,
    limit: int | None = None,
    slug: str | None = None,
    order_file: str | None = None,
    only_order: bool = False,
    use_llm: bool = False,
    seo: bool = True,
    seo_prompt: str = DEFAULT_SEO_PROMPT,
    exam_db: str | None = DEFAULT_EXAM_DB,
    nested_preserve: tuple[str, ...] = (),
    workers: int = 4,
    rate_limit: float = 0.0,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    timeout: float | None = 120.0,
    fallback: bool = False,
    on_progress: ProgressCallback | None = None,
    on_start: StartCallback | None = None,
    on_retry: RetryCallback | None = None,
    on_provider_switch: ProviderSwitchCallback | None = None,
) -> reporting.FrontmatterRunReport:
    """Validate and correct frontmatter for every content file under ``input_dir``.

    The schema is loaded from ``prompt_path``. Valid exam references are detected and
    written to ``examCodes`` (using the ``exam_db`` registry for names). A slug ->
    integer ``order_file`` sets the ``order`` field (merged into a full pass, or the
    only change with ``only_order``). ``title`` / ``seoTitle`` / ``seoDescription`` that
    are missing or out of range are rewritten with the SEO prompt (``seo``, default on;
    ``--no-seo`` leaves them flagged). ``use_llm`` additionally fills other fields the
    rules cannot infer. Nothing is written unless ``apply`` is set, and then under
    ``output_dir`` (the input directory is never modified).
    """
    schema = load_frontmatter_schema(prompt_path)
    order_map = _load_order_file(order_file) if order_file else None
    if only_order and not order_map:
        raise ValueError("--only-order requires --order-file")
    exam_registry = load_exam_registry(exam_db) if exam_db else []

    files = discover_documents(input_dir)
    if slug:
        files = [
            path
            for path in files
            if os.path.splitext(os.path.basename(path))[0] == slug
            or source_slug(load_document(path)) == slug
        ]
    if limit is not None:
        files = files[:limit]

    report = reporting.FrontmatterRunReport(
        input_dir=input_dir,
        output_dir=output_dir or "",
        schema=schema.name,
        order_file=order_file or "",
        only_order=only_order,
    )
    report.files_scanned = len(files)

    if order_map:
        stems = {os.path.splitext(os.path.basename(path))[0] for path in discover_documents(input_dir)}
        report.extra["missing_order_slugs"] = sorted(set(order_map) - stems)

    if not files:
        json_report, diff_report, markdown_report = reporting.write_frontmatter_report(report, reports_dir)
        report.extra.update(
            {"json_report": json_report, "diff_report": diff_report, "markdown_report": markdown_report}
        )
        return report

    llm = None
    seo_llm = None
    seo_spec = None
    if not only_order and (use_llm or seo):
        model = _build_llm(rate_limit, timeout, fallback, on_provider_switch)
        if use_llm:
            llm = model
        if seo:
            seo_llm = model
            seo_spec = load_seo_spec(seo_prompt)

    workers = max(1, workers)

    def _record(index: int, count: int, outcome: FrontmatterOutcome) -> None:
        if outcome.error:
            change = reporting.FrontmatterChange(slug=outcome.slug, path=outcome.path, error=outcome.error)
        else:
            result = outcome.result
            rel = os.path.relpath(outcome.path, input_dir)
            after = result.new_text
            diff = reporting.make_diff(rel, outcome.before, after) if outcome.before != after else ""
            change = reporting.FrontmatterChange(
                slug=result.slug,
                path=outcome.path,
                notes=result.notes,
                warnings=result.warnings,
                review_items=result.review_items,
                field_changes=result.changes,
                diff=diff,
            )
            if apply and output_dir and diff:
                _write_corrected(outcome.path, input_dir, output_dir, after)
        report.changes.append(change)
        try:
            reporting.write_frontmatter_report(report, reports_dir)
        except OSError:
            pass  # a partial-write failure must not abort the run
        if on_progress:
            on_progress(index, count, outcome)

    if workers == 1:
        for index, path in enumerate(files, start=1):
            outcome = _fix_source(
                path,
                index,
                len(files),
                schema,
                order_map,
                only_order,
                llm,
                max_attempts,
                exam_registry,
                seo_llm,
                seo_spec,
                nested_preserve,
                on_start,
                on_retry,
            )
            _record(index, len(files), outcome)
    else:
        _run_frontmatter_concurrently(
            files,
            schema,
            order_map,
            only_order,
            llm,
            workers,
            max_attempts,
            exam_registry,
            seo_llm,
            seo_spec,
            nested_preserve,
            _record,
            on_start,
            on_retry,
        )

    json_report, diff_report, markdown_report = reporting.write_frontmatter_report(report, reports_dir)
    report.extra.update(
        {"json_report": json_report, "diff_report": diff_report, "markdown_report": markdown_report}
    )
    return report


@dataclass
class FactOutcome:
    """Result of fact-checking one document."""

    slug: str
    path: str
    result: object | None = None
    before: str = ""
    error: str | None = None


def _fact_source(
    path: str,
    index: int,
    total: int,
    registry: FactRegistry,
    fix: bool,
    llm,
    prompt: str,
    max_attempts: int,
    on_start: StartCallback | None = None,
    on_retry: RetryCallback | None = None,
    exam_binding: tuple[str, ...] | None = None,
    vendor_contamination: bool = True,
) -> FactOutcome:
    """Fact-check one document. Never raises."""
    doc = load_document(path)
    current_slug = source_slug(doc)
    if on_start:
        on_start(index, total, current_slug)

    def _report_retry(attempt: int, attempts: int, wait: float, exc: Exception) -> None:
        if on_retry:
            on_retry(index, total, current_slug, attempt, attempts, wait, exc)

    try:
        result = generate_factcheck(
            doc,
            current_slug,
            registry,
            fix=fix,
            llm=llm,
            prompt=prompt,
            exam_binding=exam_binding,
            vendor_contamination=vendor_contamination,
            max_attempts=max_attempts,
            on_retry=_report_retry,
        )
    except FatalLLMError:
        raise
    except Exception as exc:  # noqa: BLE001 - surface any per-file failure in the report
        return FactOutcome(slug=current_slug, path=path, error=f"{type(exc).__name__}: {exc}")
    return FactOutcome(slug=current_slug, path=path, result=result, before=doc.render())


def _run_facts_concurrently(
    files,
    registry: FactRegistry,
    fix: bool,
    llm,
    prompt: str,
    workers: int,
    max_attempts: int,
    on_progress: ProgressCallback | None = None,
    on_start: StartCallback | None = None,
    on_retry: RetryCallback | None = None,
    exam_binding: tuple[str, ...] | None = None,
    vendor_contamination: bool = True,
) -> list[FactOutcome]:
    outcomes: list[FactOutcome | None] = [None] * len(files)
    total = len(files)
    completed = 0
    executor = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = {
            executor.submit(
                _fact_source,
                path,
                index,
                total,
                registry,
                fix,
                llm,
                prompt,
                max_attempts,
                on_start,
                on_retry,
                exam_binding,
                vendor_contamination,
            ): index
            for index, path in enumerate(files, start=1)
        }
        for future in as_completed(futures):
            outcome = future.result()
            outcomes[futures[future] - 1] = outcome
            completed += 1
            if on_progress:
                on_progress(completed, total, outcome)
    except BaseException:
        executor.shutdown(wait=False, cancel_futures=True)
        raise
    else:
        executor.shutdown(wait=True)
    return [outcome for outcome in outcomes if outcome is not None]


def run_check_facts(
    input_dir: str,
    output_dir: str | None = "output_factchecked",
    reports_dir: str = "reports",
    facts_dir: str = DEFAULT_FACTS_DIR,
    prompt_path: str = DEFAULT_FACT_PROMPT,
    apply: bool = False,
    fix: bool = True,
    limit: int | None = None,
    slug: str | None = None,
    use_llm: bool = False,
    workers: int = 4,
    rate_limit: float = 0.0,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    timeout: float | None = 120.0,
    fallback: bool = False,
    exam_binding: tuple[str, ...] | None = None,
    vendor_contamination: bool = True,
    on_progress: ProgressCallback | None = None,
    on_start: StartCallback | None = None,
    on_retry: RetryCallback | None = None,
    on_provider_switch: ProviderSwitchCallback | None = None,
) -> reporting.FactCheckRunReport:
    """Validate content against the verified fact files under ``facts_dir``.

    Files are linked to exams by their frontmatter ``examCodes`` or, when absent, by
    detecting exam codes/names from the fact registry. Deterministic discrepancies
    (names, services, price, passing score, duration, questions, dates, status) are
    corrected when ``fix`` is set and flagged otherwise. An LLM reviews each matched
    exam when ``use_llm`` is set. Nothing is written unless ``apply`` is set, and then
    under ``output_dir`` (the input directory is never modified).
    """
    registry = FactRegistry(load_fact_registry(facts_dir))
    prompt = load_fact_prompt(prompt_path) if use_llm else ""

    files = discover_documents(input_dir)
    if slug:
        files = [
            path
            for path in files
            if os.path.splitext(os.path.basename(path))[0] == slug
            or source_slug(load_document(path)) == slug
        ]
    if limit is not None:
        files = files[:limit]

    report = reporting.FactCheckRunReport(
        input_dir=input_dir,
        output_dir=output_dir or "",
        facts_dir=facts_dir,
    )
    report.files_scanned = len(files)
    report.extra["facts_loaded"] = len(registry.facts)

    if not files:
        json_report, diff_report, markdown_report = reporting.write_factcheck_report(report, reports_dir)
        report.extra.update(
            {"json_report": json_report, "diff_report": diff_report, "markdown_report": markdown_report}
        )
        return report

    llm = _build_llm(rate_limit, timeout, fallback, on_provider_switch) if use_llm else None

    workers = max(1, workers)

    def _record(index: int, count: int, outcome: FactOutcome) -> None:
        if outcome.error:
            change = reporting.FactCheckChange(slug=outcome.slug, path=outcome.path, error=outcome.error)
        else:
            result = outcome.result
            rel = os.path.relpath(outcome.path, input_dir)
            after = result.new_text
            diff = reporting.make_diff(rel, outcome.before, after) if outcome.before != after else ""
            change = reporting.FactCheckChange(
                slug=result.slug,
                path=outcome.path,
                exams=result.exams,
                findings=result.findings,
                review_items=result.review_items,
                warnings=result.warnings,
                sources=list(dict.fromkeys(result.sources)),
                diff=diff,
            )
            if apply and output_dir and diff:
                _write_corrected(outcome.path, input_dir, output_dir, after)
        report.changes.append(change)
        try:
            reporting.write_factcheck_report(report, reports_dir)
        except OSError:
            pass  # a partial-write failure must not abort the run
        if on_progress:
            on_progress(index, count, outcome)

    if workers == 1:
        for index, path in enumerate(files, start=1):
            outcome = _fact_source(
                path,
                index,
                len(files),
                registry,
                fix,
                llm,
                prompt,
                max_attempts,
                on_start,
                on_retry,
                exam_binding,
                vendor_contamination,
            )
            _record(index, len(files), outcome)
    else:
        _run_facts_concurrently(
            files,
            registry,
            fix,
            llm,
            prompt,
            workers,
            max_attempts,
            _record,
            on_start,
            on_retry,
            exam_binding,
            vendor_contamination,
        )

    json_report, diff_report, markdown_report = reporting.write_factcheck_report(report, reports_dir)
    report.extra.update(
        {"json_report": json_report, "diff_report": diff_report, "markdown_report": markdown_report}
    )
    return report


def _human_review_entry(result, rating, threshold: float) -> dict:
    """Flatten a corrected+reviewed post into the human-review report entry."""
    low = [
        f"{criterion.label} ({criterion.weight}): {rating.scores.get(criterion.key, 0):.0f}"
        for criterion in rating.criteria
        if rating.scores.get(criterion.key, 5) <= 2
    ]
    return {
        "slug": result.slug,
        "grade": rating.grade,
        "average": rating.average,
        "weaknesses": list(rating.weaknesses),
        "factual_issues": list(rating.factual_issues),
        "low_criteria": low,
        "changed": list(result.notes),
        "warnings": list(result.warnings),
        "sources": list(result.sources),
    }