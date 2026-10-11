"""CLI entrypoint for contentos."""

from __future__ import annotations

import os
import sys
import threading
import time

import typer

from contentos.agents.corrector import DEFAULT_CORRECTION_PROMPT
from contentos.agents.factchecker import DEFAULT_PROMPT_PATH as DEFAULT_FACT_PROMPT
from contentos.agents.frontmatter import DEFAULT_PROMPT_PATH as DEFAULT_FRONTMATTER_PROMPT
from contentos.agents.frontmatter import DEFAULT_SEO_PROMPT
from contentos.agents.rater import DEFAULT_PROMPT_PATH as DEFAULT_RATING_PROMPT
from contentos.agents.rater import load_rubric
from contentos.core import reporting
from contentos.core.llm import (
    FatalLLMError,
    deepseek_available,
    deepseek_model_name,
    default_max_searches,
    gemini_backend,
    gemini_model_name,
    ping_llm,
)
from contentos.core.pipeline import (
    DEFAULT_SEARCH_TIMEOUT,
    run_check_facts,
    run_correct_blogs,
    run_fix_frontmatter,
    run_interlink,
    run_link_strip,
    run_rate_blogs,
    run_review_interlinks,
)
from contentos.core.profiles import (
    DEFAULT_MANIFEST_PATH,
    ResolvedProfile,
    load_manifest,
    resolve_profile,
)

app = typer.Typer(
    name="contentos",
    help="Agent-based content pipeline for SEO content (write / review / interlink).",
    no_args_is_help=True,
)


@app.callback(invoke_without_command=False)
def main() -> None:
    """contentos — content pipeline agents."""


def _summarize(report: reporting.RunReport, dry_run: bool) -> None:
    typer.echo(f"[{report.agent}] scanned={report.files_scanned} changed={report.files_changed}")
    mode = "dry-run" if dry_run else "apply"
    for change in report.changes:
        if not change.diff:
            continue
        label = change.path
        detail = []
        if change.removed_lines:
            detail.append(f"-{change.removed_lines} lines")
        if change.notes:
            detail.append(f"{len(change.notes)} notes")
        typer.echo(f"    ~ {label} ({', '.join(detail)})")
    typer.echo(f"Reports written ({mode}):")
    typer.echo(f"    json: {report.extra.get('json_report')}")
    typer.echo(f"    diff: {report.extra.get('diff_report')}")


def _resolve_profile(
    input_dir: str, content_type: str | None, profiles_path: str
) -> ResolvedProfile | None:
    """Resolve the content-type profile for ``input_dir``.

    Phase 0 foundation: the profile is resolved and validated so agents can adopt it.
    Inference from the folder is best-effort and never blocks a run; an explicit
    ``--content-type`` makes a missing/invalid manifest or unknown type a hard error.
    """
    if profiles_path != DEFAULT_MANIFEST_PATH and not os.path.isfile(profiles_path):
        typer.echo(f"--profiles file not found: {profiles_path}", err=True)
        raise typer.Exit(1)
    try:
        manifest = load_manifest(profiles_path)
    except FileNotFoundError as exc:
        if content_type:
            typer.echo(f"--content-type requires a manifest at {profiles_path}", err=True)
            raise typer.Exit(1) from exc
        return None
    except (ValueError, TypeError) as exc:
        typer.echo(f"Invalid manifest {profiles_path}: {exc}", err=True)
        raise typer.Exit(1) from exc
    try:
        resolved = resolve_profile(input_dir, content_type, manifest)
    except ValueError as exc:
        if content_type:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1) from exc
        return None
    return resolved


def _require_dir(path: str, option: str) -> None:
    """Fail fast with a clear message when an input directory does not exist."""
    if not os.path.isdir(path):
        typer.echo(f"{option}: directory not found: {path}", err=True)
        raise typer.Exit(1)


def _require_file(path: str, option: str) -> None:
    """Fail fast with a clear message when a required file does not exist."""
    if not os.path.isfile(path):
        typer.echo(f"{option}: file not found: {path}", err=True)
        raise typer.Exit(1)


def _require_optional_file(path: str | None, option: str) -> None:
    """Validate a file only when the option was supplied."""
    if path:
        _require_file(path, option)


def _announce_profile(resolved: ResolvedProfile | None, content_type: str | None) -> None:
    """Echo the resolved content type when the user asked for one explicitly."""
    if not content_type or resolved is None:
        return
    exam = f" (exam: {resolved.exam_slug})" if resolved.exam_slug else ""
    typer.echo(f"content-type: {resolved.content_type}{exam}")


@app.command("strip-links")
def strip_links(
    input_dir: str = typer.Option("blogs", "--input", "-i", help="Input folder of content files."),
    output_dir: str | None = typer.Option(None, "--output", help="Where cleaned files go (apply mode)."),
    reports_dir: str = typer.Option("reports", "--reports", help="Where reports go."),
    profiles: str = typer.Option(
        DEFAULT_MANIFEST_PATH, "--profiles", help="Content-type manifest."
    ),
    content_type: str | None = typer.Option(
        None, "--content-type", help="Override the content type (else inferred from the input folder)."
    ),
    apply: bool = typer.Option(False, "--apply", help="Write cleaned files to --output."),
) -> None:
    """Agent 1: remove all hyperlinks and link-header blocks."""
    if apply and not output_dir:
        typer.echo("--apply requires --output <dir>", err=True)
        raise typer.Exit(1)
    _require_dir(input_dir, "--input")
    resolved = _resolve_profile(input_dir, content_type, profiles)
    _announce_profile(resolved, content_type)
    if resolved is not None and not resolved.profile.strip_links:
        typer.echo(
            f"content-type {resolved.content_type}: link stripping is disabled; sources and URLs must survive."
        )
        return
    report = run_link_strip(
        input_dir=input_dir,
        output_dir=output_dir,
        reports_dir=reports_dir,
        apply=apply,
    )
    _summarize(report, dry_run=not apply)


@app.command("interlink")
def interlink(
    input_dir: str = typer.Option("blogs", "--input", "-i", help="Input folder of content files."),
    inventory: str = typer.Option(
        "input/content_inventory.json", "--inventory", help="Path to content_inventory.json."
    ),
    output: str = typer.Option(
        "output/interlinks.json",
        "--output",
        help="Mapping FILE to write (apply mode). A directory gets interlinks.json inside it.",
    ),
    reports_dir: str = typer.Option("reports", "--reports", help="Where reports go."),
    profiles: str = typer.Option(
        DEFAULT_MANIFEST_PATH, "--profiles", help="Content-type manifest."
    ),
    content_type: str | None = typer.Option(
        None, "--content-type", help="Override the content type (else inferred from the input folder)."
    ),
    apply: bool = typer.Option(False, "--apply", help="Write the link mapping to --output."),
    limit: int | None = typer.Option(None, "--limit", help="Process only the first N sources."),
    slug: str | None = typer.Option(None, "--slug", help="Process a single source slug."),
    workers: int = typer.Option(4, "--workers", "-w", help="Concurrent LLM worker threads."),
    rate_limit: float = typer.Option(
        0.0, "--rate-limit", help="Max LLM requests per second (0 = unlimited)."
    ),
    max_attempts: int = typer.Option(
        5, "--max-attempts", help="Retries per LLM call on 429/5xx/timeouts."
    ),
    timeout: float = typer.Option(120.0, "--timeout", help="Per-request LLM timeout in seconds."),
    fallback: bool = typer.Option(
        True,
        "--fallback/--no-fallback",
        help="Switch to DeepSeek if Gemini starts timing out.",
    ),
    ping: bool = typer.Option(True, "--ping/--no-ping", help="Verify LLM connectivity before the run."),
    quiet: bool = typer.Option(
        False, "--quiet", "-q", help="Hide live progress; print a detailed summary at the end."
    ),
) -> None:
    """Agent 2: generate internal-link mappings from the content inventory."""
    _require_dir(input_dir, "--input")
    _require_file(inventory, "--inventory")
    _announce_profile(_resolve_profile(input_dir, content_type, profiles), content_type)
    started = time.monotonic()
    log_lock = threading.Lock()

    def _log(message: str) -> None:
        with log_lock:
            print(message, file=sys.stderr, flush=True)

    def _start(index: int, count: int, slug: str) -> None:
        _log(f"  -> [{index}/{count}] reviewing {slug} ...")

    def _retry(index: int, count: int, slug: str, attempt: int, attempts: int, wait: float, exc: Exception) -> None:
        _log(
            f"  !! [{index}/{count}] {slug}: retry {attempt}/{attempts - 1} in {wait:.0f}s "
            f"({type(exc).__name__})"
        )

    def _progress(index: int, count: int, outcome) -> None:
        elapsed = time.monotonic() - started
        rate = index / elapsed if elapsed > 0 else 0.0
        remaining = (count - index) / rate if rate > 0 else 0.0
        stamp = f"[{index:>{len(str(count))}}/{count}] {_fmt_duration(elapsed)} elapsed"
        if remaining > 0:
            stamp += f", ~{_fmt_duration(remaining)} left"
        if outcome.error:
            _log(f"{stamp}  FAILED {outcome.slug}: {outcome.error}")
            return
        exam = getattr(outcome.analysis, "primary_exam", None) or "no exam"
        warn = f", {len(outcome.warnings)} warnings" if outcome.warnings else ""
        _log(f"{stamp}  OK {outcome.slug} [{exam}] -> {len(outcome.links)} links{warn}")

    def _provider_switch(provider: str, exc: Exception) -> None:
        _log(
            f"  ** Gemini {type(exc).__name__}: falling back to {provider} for this request."
        )

    use_fallback = fallback
    if not quiet:
        fallback_note = "off"
        if use_fallback:
            fallback_note = f"deepseek ({deepseek_model_name()})" if deepseek_available() else "off"
        _log(
            f"Starting interlink run: model={gemini_model_name()} ({gemini_backend()}), workers={workers}, "
            f"rate-limit={rate_limit if rate_limit > 0 else 'unlimited'}, "
            f"max-attempts={max_attempts}, timeout={timeout:.0f}s, fallback={fallback_note}"
        )
        if use_fallback and not deepseek_available():
            _log("  note: DEEPSEEK_API_KEY is not set, so no fallback provider is available.")

    if ping:
        if not quiet:
            _log("Checking LLM connectivity ...")
        try:
            reply = ping_llm(timeout=min(timeout, 30.0))
        except Exception as exc:
            typer.echo(
                f"LLM connectivity check failed: {type(exc).__name__}: {exc}\n"
                "Check GEMINI_BACKEND, GOOGLE_CLOUD_PROJECT / GEMINI_API_KEY and GEMINI_MODEL. "
                "For Vertex ensure ADC is set (`gcloud auth application-default login` or "
                "GOOGLE_APPLICATION_CREDENTIALS), or rerun with --no-ping to skip this check.",
                err=True,
            )
            raise typer.Exit(1) from exc
        if not quiet:
            _log(f"LLM OK: {reply[:80] or '(empty response)'}")

    try:
        report = run_interlink(
            input_dir=input_dir,
            inventory_path=inventory,
            output_path=output,
            reports_dir=reports_dir,
            apply=apply,
            limit=limit,
            slug=slug,
            workers=workers,
            rate_limit=rate_limit,
            max_attempts=max_attempts,
            timeout=timeout,
            fallback=use_fallback,
            on_progress=None if quiet else _progress,
            on_start=None if quiet else _start,
            on_retry=None if quiet else _retry,
            on_provider_switch=_provider_switch,
        )
    except KeyboardInterrupt:
        _log("\nInterrupted by user. Exiting without writing output.")
        os._exit(130)
    except FatalLLMError as exc:
        typer.echo(f"Fatal LLM error, aborting: {exc}", err=True)
        raise typer.Exit(1) from exc
    except ValueError as exc:
        typer.echo(f"Interlink failed: {exc}", err=True)
        raise typer.Exit(1) from exc

    elapsed = time.monotonic() - started
    mode = "apply" if apply else "dry-run"
    typer.echo(
        f"[interlink] scanned={report.files_scanned} processed={report.files_processed} "
        f"links={report.total_links} ({mode}, {_fmt_duration(elapsed)})"
    )

    if quiet:
        for source in report.sources:
            if source.error:
                typer.echo(f"    ! {source.slug}: {source.error}")
                continue
            exam = source.primary_exam or "no exam code"
            typer.echo(f"    ~ {source.slug} [{exam}]: {len(source.links)} links")
            for warning in source.warnings:
                typer.echo(f"        - {warning}")
    else:
        failed = [source for source in report.sources if source.error]
        if failed:
            typer.echo(f"Failed sources ({len(failed)}):")
            for source in failed:
                typer.echo(f"    ! {source.slug}: {source.error}")

    if report.sources_without_exam:
        typer.echo("Sources without an exam code (flagged):")
        for flagged in report.sources_without_exam:
            typer.echo(f"    ? {flagged}")

    typer.echo(f"Report: {report.extra.get('json_report')}")
    if apply:
        typer.echo(f"Wrote: {report.extra.get('output')}")
    else:
        typer.echo("No mapping written (dry-run). Use --apply to write it.")


@app.command("review-interlinks")
def review_interlinks(
    mapping: str = typer.Option(
        "output/interlinks.json", "--mapping", help="Existing interlinks.json to review."
    ),
    input_dir: str = typer.Option("blogs", "--input", "-i", help="Source content folder (for redo)."),
    inventory: str = typer.Option(
        "input/content_inventory.json", "--inventory", help="Path to content_inventory.json."
    ),
    output: str | None = typer.Option(
        None, "--output", help="Where the fixed mapping goes (defaults to --mapping)."
    ),
    reports_dir: str = typer.Option("reports", "--reports", help="Where reports go."),
    apply: bool = typer.Option(False, "--apply", help="Write the fixed mapping to --output."),
    redo: bool = typer.Option(
        True, "--redo/--no-redo", help="Re-run the LLM for sources with a single related link."
    ),
    single_related: int = typer.Option(
        1, "--single-related", help="Redo sources whose related count equals this."
    ),
    workers: int = typer.Option(4, "--workers", "-w", help="Concurrent LLM worker threads."),
    rate_limit: float = typer.Option(
        0.0, "--rate-limit", help="Max LLM requests per second (0 = unlimited)."
    ),
    max_attempts: int = typer.Option(
        5, "--max-attempts", help="Retries per LLM call on 429/5xx/timeouts."
    ),
    timeout: float = typer.Option(120.0, "--timeout", help="Per-request LLM timeout in seconds."),
    fallback: bool = typer.Option(
        True, "--fallback/--no-fallback", help="Switch to DeepSeek if Gemini times out."
    ),
    ping: bool = typer.Option(True, "--ping/--no-ping", help="Verify LLM connectivity before the run."),
    limit: int | None = typer.Option(None, "--limit", help="Redo only the first N flagged sources."),
    slug: str | None = typer.Option(None, "--slug", help="Redo a single flagged source slug."),
    quiet: bool = typer.Option(
        False, "--quiet", "-q", help="Hide live progress; print a detailed summary at the end."
    ),
) -> None:
    """Review an interlinks mapping: fix reciprocal links, 100% and quiz positions."""
    _require_file(mapping, "--mapping")
    _require_file(inventory, "--inventory")
    if redo:
        _require_dir(input_dir, "--input")
    started = time.monotonic()
    log_lock = threading.Lock()

    def _log(message: str) -> None:
        with log_lock:
            print(message, file=sys.stderr, flush=True)

    def _start(index: int, count: int, slug: str) -> None:
        _log(f"  -> [{index}/{count}] redoing {slug} ...")

    def _retry(index: int, count: int, slug: str, attempt: int, attempts: int, wait: float, exc: Exception) -> None:
        _log(
            f"  !! [{index}/{count}] {slug}: retry {attempt}/{attempts - 1} in {wait:.0f}s "
            f"({type(exc).__name__})"
        )

    def _progress(index: int, count: int, outcome) -> None:
        if outcome.error:
            _log(f"[{index}/{count}] FAILED {outcome.slug}: {outcome.error}")
            return
        warn = f", {len(outcome.warnings)} warnings" if outcome.warnings else ""
        _log(f"[{index}/{count}] OK {outcome.slug} -> {len(outcome.links)} links{warn}")

    def _provider_switch(provider: str, exc: Exception) -> None:
        _log(
            f"  ** Gemini {type(exc).__name__}: falling back to {provider} for this request."
        )

    use_fallback = fallback
    if ping and redo:
        if not quiet:
            _log("Checking LLM connectivity ...")
        try:
            reply = ping_llm(timeout=min(timeout, 30.0))
        except Exception as exc:
            typer.echo(
                f"LLM connectivity check failed: {type(exc).__name__}: {exc}\n"
                "Rerun with --no-ping to skip this check or --no-redo for deterministic fixes only.",
                err=True,
            )
            raise typer.Exit(1) from exc
        if not quiet:
            _log(f"LLM OK: {reply[:80] or '(empty response)'}")

    try:
        report = run_review_interlinks(
            mapping_path=mapping,
            input_dir=input_dir,
            inventory_path=inventory,
            output_path=output,
            reports_dir=reports_dir,
            apply=apply,
            redo=redo,
            single_related=single_related,
            workers=workers,
            rate_limit=rate_limit,
            max_attempts=max_attempts,
            timeout=timeout,
            fallback=use_fallback,
            limit=limit,
            slug=slug,
            on_progress=None if quiet else _progress,
            on_start=None if quiet else _start,
            on_retry=None if quiet else _retry,
            on_provider_switch=_provider_switch,
        )
    except KeyboardInterrupt:
        _log("\nInterrupted by user. Exiting without writing output.")
        os._exit(130)
    except FatalLLMError as exc:
        typer.echo(f"Fatal LLM error, aborting: {exc}", err=True)
        raise typer.Exit(1) from exc
    except (ValueError, TypeError, FileNotFoundError) as exc:
        typer.echo(f"Review failed: {exc}", err=True)
        raise typer.Exit(1) from exc

    elapsed = time.monotonic() - started
    mode = "apply" if apply else "dry-run"
    typer.echo(
        f"[review-interlinks] scanned={report.sources_scanned} "
        f"incomplete-fixed={len(report.incomplete_fixed)} "
        f"anchors-fixed={len(report.anchors_fixed)} "
        f"reciprocal-dropped={len(report.reciprocal_dropped)} "
        f"promoted={len(report.promoted_to_related)} quizzes-fixed={len(report.quizzes_fixed)} "
        f"redone={len(report.redo_results)} ({mode}, {_fmt_duration(elapsed)})"
    )
    for note in report.incomplete_fixed:
        typer.echo(f"    - {note}")
    for note in report.anchors_fixed:
        typer.echo(f"    - {note}")
    for note in report.reciprocal_dropped:
        typer.echo(f"    - {note}")
    for note in report.promoted_to_related:
        typer.echo(f"    - {note}")
    for note in report.quizzes_fixed:
        typer.echo(f"    - {note}")
    for source in report.redo_results:
        if source.error:
            typer.echo(f"    ! {source.slug}: {source.error}")
        else:
            typer.echo(f"    ~ redid {source.slug} -> {len(source.links)} links")

    typer.echo(f"Report: {report.extra.get('json_report')}")
    if apply:
        typer.echo(f"Wrote: {report.extra.get('output')}")
    else:
        typer.echo("No mapping written (dry-run). Use --apply to write it.")


@app.command("rate-blogs")
def rate_blogs(
    input_dir: str = typer.Option(
        ..., "--input", "-i", help="Folder of content files to rate (required)."
    ),
    reports_dir: str = typer.Option("reports", "--reports", help="Where reports go."),
    profiles: str = typer.Option(
        DEFAULT_MANIFEST_PATH, "--profiles", help="Content-type manifest."
    ),
    content_type: str | None = typer.Option(
        None, "--content-type", help="Override the content type (else inferred from the input folder)."
    ),
    prompt: str | None = typer.Option(
        None,
        "--prompt",
        help="Rubric prompt file (YAML frontmatter + system prompt). Defaults to the content type's rubric.",
    ),
    limit: int | None = typer.Option(None, "--limit", help="Rate only the first N files."),
    slug: str | None = typer.Option(None, "--slug", help="Rate a single file slug."),
    workers: int = typer.Option(4, "--workers", "-w", help="Concurrent LLM worker threads."),
    rate_limit: float = typer.Option(
        0.0, "--rate-limit", help="Max LLM requests per second (0 = unlimited)."
    ),
    max_attempts: int = typer.Option(
        5, "--max-attempts", help="Retries per LLM call on 429/5xx/timeouts."
    ),
    timeout: float = typer.Option(120.0, "--timeout", help="Per-request LLM timeout in seconds."),
    search_timeout: float = typer.Option(
        DEFAULT_SEARCH_TIMEOUT,
        "--search-timeout",
        help="Per-request timeout for search-grounded calls (grounding is slower).",
    ),
    fallback: bool = typer.Option(
        True, "--fallback/--no-fallback", help="Switch to DeepSeek if Gemini times out."
    ),
    verify_facts: bool = typer.Option(
        True,
        "--verify-facts/--no-verify-facts",
        help="Search-verify exam-specific posts and any [UNVERIFIABLE] claims.",
    ),
    max_searches: int = typer.Option(
        default_max_searches(),
        "--max-searches",
        help="Max search-grounded calls for the whole run (0 = unlimited). "
        "Defaults to MAX_WEB_SEARCHES or 1500.",
    ),
    ping: bool = typer.Option(True, "--ping/--no-ping", help="Verify LLM connectivity before the run."),
    quiet: bool = typer.Option(
        False, "--quiet", "-q", help="Hide live progress; print a detailed summary at the end."
    ),
) -> None:
    """Blog rating agent: grade each post A+ to F against the ExamOS rubric."""
    _require_dir(input_dir, "--input")
    resolved = _resolve_profile(input_dir, content_type, profiles)
    _announce_profile(resolved, content_type)
    profile = resolved.profile if resolved else None
    prompt = prompt or (profile.rating_prompt if profile else None) or DEFAULT_RATING_PROMPT
    _require_file(prompt, "--prompt")
    force_ground = bool(profile and profile.ground_always)
    started = time.monotonic()
    log_lock = threading.Lock()

    def _log(message: str) -> None:
        with log_lock:
            print(message, file=sys.stderr, flush=True)

    def _start(index: int, count: int, slug: str) -> None:
        _log(f"  -> [{index}/{count}] rating {slug} ...")

    def _retry(index: int, count: int, slug: str, attempt: int, attempts: int, wait: float, exc: Exception) -> None:
        _log(
            f"  !! [{index}/{count}] {slug}: retry {attempt}/{attempts - 1} in {wait:.0f}s "
            f"({type(exc).__name__})"
        )

    def _progress(index: int, count: int, outcome) -> None:
        elapsed = time.monotonic() - started
        rate = index / elapsed if elapsed > 0 else 0.0
        remaining = (count - index) / rate if rate > 0 else 0.0
        stamp = f"[{index:>{len(str(count))}}/{count}] {_fmt_duration(elapsed)} elapsed"
        if remaining > 0:
            stamp += f", ~{_fmt_duration(remaining)} left"
        if outcome.error:
            _log(f"{stamp}  FAILED {outcome.slug}: {outcome.error}")
            return
        grade = outcome.result.grade
        average = outcome.result.average
        _log(f"{stamp}  OK {outcome.slug} [{grade} {average:.2f}]")

    def _provider_switch(provider: str, exc: Exception) -> None:
        _log(
            f"  ** Gemini {type(exc).__name__}: falling back to {provider} for this request."
        )

    use_fallback = fallback
    if not quiet:
        fallback_note = "off"
        if use_fallback:
            fallback_note = f"deepseek ({deepseek_model_name()})" if deepseek_available() else "off"
        _log(
            f"Starting rating run: model={gemini_model_name()} ({gemini_backend()}), workers={workers}, "
            f"rate-limit={rate_limit if rate_limit > 0 else 'unlimited'}, "
            f"max-attempts={max_attempts}, timeout={timeout:.0f}s, fallback={fallback_note}, "
            f"max-searches={max_searches if verify_facts else 'off'}"
        )

    if ping:
        if not quiet:
            _log("Checking LLM connectivity ...")
        try:
            reply = ping_llm(timeout=min(timeout, 30.0))
        except Exception as exc:
            typer.echo(
                f"LLM connectivity check failed: {type(exc).__name__}: {exc}\n"
                "Check GEMINI_BACKEND, GOOGLE_CLOUD_PROJECT / GEMINI_API_KEY and GEMINI_MODEL. "
                "For Vertex ensure ADC is set (`gcloud auth application-default login` or "
                "GOOGLE_APPLICATION_CREDENTIALS), or rerun with --no-ping to skip this check.",
                err=True,
            )
            raise typer.Exit(1) from exc
        if not quiet:
            _log(f"LLM OK: {reply[:80] or '(empty response)'}")

    try:
        report = run_rate_blogs(
            input_dir=input_dir,
            reports_dir=reports_dir,
            prompt_path=prompt,
            limit=limit,
            slug=slug,
            workers=workers,
            rate_limit=rate_limit,
            max_attempts=max_attempts,
            timeout=timeout,
            search_timeout=search_timeout,
            fallback=use_fallback,
            verify_facts=verify_facts,
            force_ground=force_ground,
            max_searches=max_searches,
            on_progress=None if quiet else _progress,
            on_start=None if quiet else _start,
            on_retry=None if quiet else _retry,
            on_provider_switch=_provider_switch,
        )
    except KeyboardInterrupt:
        _log(
            f"\nInterrupted by user. Partial results ({reports_dir}\\rating-*.md/.json) "
            "contain every rating finished so far."
        )
        os._exit(130)
    except FatalLLMError as exc:
        typer.echo(f"Fatal LLM error, aborting: {exc}", err=True)
        raise typer.Exit(1) from exc
    except (ValueError, FileNotFoundError) as exc:
        typer.echo(f"Rating failed: {exc}", err=True)
        raise typer.Exit(1) from exc

    elapsed = time.monotonic() - started
    search_note = (
        f"web-searches={report.searches}/{report.max_searches}"
        if report.max_searches > 0
        else f"web-searches={report.searches}"
    )
    typer.echo(
        f"[rate-blogs] scanned={report.files_scanned} rated={report.files_rated} "
        f"failed={len(report.errors)} average={report.average:.2f} "
        f"{search_note} ({_fmt_duration(elapsed)})"
    )
    if report.grade_counts:
        counts = ", ".join(f"{grade}={count}" for grade, count in sorted(report.grade_counts.items()))
        typer.echo(f"    grades: {counts}")
    if quiet:
        for record in report.ratings:
            typer.echo(f"    ~ {record['slug']} [{record['grade']} {record['average']:.2f}]")
        for record in report.errors:
            typer.echo(f"    ! {record['slug']}: {record['error']}")
    typer.echo(f"Markdown report: {report.extra.get('markdown_report')}")
    typer.echo(f"JSON report: {report.extra.get('json_report')}")


@app.command("correct-blogs")
def correct_blogs(
    input_dir: str = typer.Option(
        ..., "--input", "-i", help="Folder of content files to correct (required)."
    ),
    output_dir: str = typer.Option(
        "output_corrected", "--output", help="Where corrected files go in apply mode."
    ),
    reports_dir: str = typer.Option("reports", "--reports", help="Where reports go."),
    profiles: str = typer.Option(
        DEFAULT_MANIFEST_PATH, "--profiles", help="Content-type manifest."
    ),
    content_type: str | None = typer.Option(
        None, "--content-type", help="Override the content type (else inferred from the input folder)."
    ),
    prompt: str | None = typer.Option(
        None,
        "--prompt",
        help="Correction prompt file (YAML frontmatter + system prompt). Defaults to the content type's prompt.",
    ),
    apply: bool = typer.Option(
        False, "--apply", help="Write corrected files to --output (never modifies --input)."
    ),
    limit: int | None = typer.Option(None, "--limit", help="Correct only the first N files."),
    slug: str | None = typer.Option(None, "--slug", help="Correct a single file slug."),
    workers: int = typer.Option(4, "--workers", "-w", help="Concurrent LLM worker threads."),
    rate_limit: float = typer.Option(
        0.0, "--rate-limit", help="Max LLM requests per second (0 = unlimited)."
    ),
    max_attempts: int = typer.Option(
        5, "--max-attempts", help="Retries per LLM call on 429/5xx/timeouts."
    ),
    timeout: float = typer.Option(120.0, "--timeout", help="Per-request LLM timeout in seconds."),
    search_timeout: float = typer.Option(
        DEFAULT_SEARCH_TIMEOUT,
        "--search-timeout",
        help="Per-request timeout for search-grounded calls (grounding is slower).",
    ),
    fallback: bool = typer.Option(
        True, "--fallback/--no-fallback", help="Switch to DeepSeek if Gemini times out."
    ),
    verify_faq: bool = typer.Option(
        True,
        "--verify-faq/--no-verify-faq",
        help="Generate or regenerate FAQ sections with Google Search grounding.",
    ),
    fix_facts: bool = typer.Option(
        True,
        "--fix-facts/--no-fix-facts",
        help="Search-verify exam posts and apply sourced factual corrections.",
    ),
    fix_grammar: bool = typer.Option(
        True,
        "--fix-grammar/--no-fix-grammar",
        help="Apply minimal fixes for sentence fragments and grammar errors.",
    ),
    fix_slop: bool = typer.Option(
        True,
        "--fix-slop/--no-fix-slop",
        help="Remove AI-slop tells (empty intensifiers, filler, scare quotes).",
    ),
    fix_examos: bool = typer.Option(
        True,
        "--fix-examos/--no-fix-examos",
        help="Fix invented ExamOS features / wrong booking triggers.",
    ),
    seo: bool = typer.Option(
        False,
        "--seo/--no-seo",
        help="Also rewrite title/seoTitle/seoDescription here. Off by default: fix-frontmatter owns SEO.",
    ),
    review: bool = typer.Option(
        True,
        "--review/--no-review",
        help="Re-rate each corrected post and flag any below the A threshold.",
    ),
    review_threshold: float = typer.Option(
        4.3,
        "--review-threshold",
        help="Average score a corrected post must reach (4.3 = the A band).",
    ),
    rating_prompt: str | None = typer.Option(
        None,
        "--rating-prompt",
        help="Rubric used for the post-correction review. Defaults to the content type's rubric.",
    ),
    ratings: str | None = typer.Option(
        None,
        "--ratings",
        help="Rating report JSON; de-slop only posts scoring below --slop-threshold.",
    ),
    slop_threshold: float = typer.Option(
        3.0,
        "--slop-threshold",
        help="Voice/AI-Slop score below which a post is de-slopped (with --ratings).",
    ),
    min_grade: str | None = typer.Option(
        None,
        "--min-grade",
        help="Only correct posts graded below this letter (e.g. 'A-'), read from --ratings.",
    ),
    min_average: float | None = typer.Option(
        None,
        "--min-average",
        help="Only correct posts whose rating average is below this (e.g. 4.0 = A-).",
    ),
    flagged_only: bool = typer.Option(
        False,
        "--flagged-only",
        help="With --ratings, process only the posts flagged for AI slop (voice_ai_slop < "
        "--slop-threshold) and skip the rest.",
    ),
    max_searches: int = typer.Option(
        default_max_searches(),
        "--max-searches",
        help="Max search-grounded calls for the whole run (0 = unlimited). "
        "Defaults to MAX_WEB_SEARCHES or 1500.",
    ),
    ping: bool = typer.Option(True, "--ping/--no-ping", help="Verify LLM connectivity before the run."),
    quiet: bool = typer.Option(
        False, "--quiet", "-q", help="Hide live progress; print a detailed summary at the end."
    ),
) -> None:
    """Blog correction agent: fix FAQ headings, endings, FAQs, facts, grammar and slop."""
    _require_dir(input_dir, "--input")
    resolved = _resolve_profile(input_dir, content_type, profiles)
    _announce_profile(resolved, content_type)
    profile = resolved.profile if resolved else None
    prompt = prompt or (profile.corrections_prompt if profile else None) or DEFAULT_CORRECTION_PROMPT
    rating_prompt = (
        rating_prompt or (profile.rating_prompt if profile else None) or DEFAULT_RATING_PROMPT
    )
    _require_file(prompt, "--prompt")
    _require_file(rating_prompt, "--rating-prompt")
    _require_optional_file(ratings, "--ratings")
    faq_applies = profile.faq.applies if profile else True
    started = time.monotonic()
    log_lock = threading.Lock()

    def _log(message: str) -> None:
        with log_lock:
            print(message, file=sys.stderr, flush=True)

    def _start(index: int, count: int, slug: str) -> None:
        _log(f"  -> [{index}/{count}] correcting {slug} ...")

    def _retry(index: int, count: int, slug: str, attempt: int, attempts: int, wait: float, exc: Exception) -> None:
        _log(
            f"  !! [{index}/{count}] {slug}: retry {attempt}/{attempts - 1} in {wait:.0f}s "
            f"({type(exc).__name__})"
        )

    def _progress(index: int, count: int, outcome) -> None:
        elapsed = time.monotonic() - started
        rate = index / elapsed if elapsed > 0 else 0.0
        remaining = (count - index) / rate if rate > 0 else 0.0
        stamp = f"[{index:>{len(str(count))}}/{count}] {_fmt_duration(elapsed)} elapsed"
        if remaining > 0:
            stamp += f", ~{_fmt_duration(remaining)} left"
        if outcome.error:
            _log(f"{stamp}  FAILED {outcome.slug}: {outcome.error}")
            return
        changed = bool(outcome.result.notes)
        _log(f"{stamp}  OK {outcome.slug} [{'changed' if changed else 'no change'}]")

    def _provider_switch(provider: str, exc: Exception) -> None:
        _log(f"  ** Gemini {type(exc).__name__}: falling back to {provider} for this request.")

    use_fallback = fallback
    if not quiet:
        fallback_note = "off"
        if use_fallback:
            fallback_note = f"deepseek ({deepseek_model_name()})" if deepseek_available() else "off"
        _log(
            f"Starting correction run: model={gemini_model_name()} ({gemini_backend()}), "
            f"workers={workers}, rate-limit={rate_limit if rate_limit > 0 else 'unlimited'}, "
            f"max-attempts={max_attempts}, timeout={timeout:.0f}s, fallback={fallback_note}, "
            f"max-searches={max_searches if verify_faq else 'off'}, "
            f"apply={'yes -> ' + output_dir if apply else 'no (dry-run)'}"
        )

    if ping:
        if not quiet:
            _log("Checking LLM connectivity ...")
        try:
            reply = ping_llm(timeout=min(timeout, 30.0))
        except Exception as exc:
            typer.echo(
                f"LLM connectivity check failed: {type(exc).__name__}: {exc}\n"
                "Check GEMINI_BACKEND, GOOGLE_CLOUD_PROJECT / GEMINI_API_KEY and GEMINI_MODEL. "
                "For Vertex ensure ADC is set (`gcloud auth application-default login` or "
                "GOOGLE_APPLICATION_CREDENTIALS), or rerun with --no-ping to skip this check.",
                err=True,
            )
            raise typer.Exit(1) from exc
        if not quiet:
            _log(f"LLM OK: {reply[:80] or '(empty response)'}")

    if (min_grade or min_average is not None) and not ratings:
        typer.echo("--min-grade/--min-average require --ratings <report.json>", err=True)
        raise typer.Exit(1)

    deslop_slugs: set[str] | None = None
    if ratings:
        try:
            scores = reporting.load_rating_scores(ratings)
        except (OSError, ValueError) as exc:
            typer.echo(f"Could not read ratings report {ratings}: {exc}", err=True)
            raise typer.Exit(1) from exc
        deslop_slugs = {
            slug
            for slug, criteria in scores.items()
            if criteria.get("voice_ai_slop", 5.0) < slop_threshold
        }
        if not quiet:
            _log(
                f"De-slop gated to {len(deslop_slugs)} post(s) scoring below "
                f"{slop_threshold} on Voice/AI Slop."
            )

    grade_slugs: set[str] | None = None
    if min_grade or min_average is not None:
        try:
            grades = reporting.load_rating_grades(ratings)
        except (OSError, ValueError) as exc:
            typer.echo(f"Could not read ratings report {ratings}: {exc}", err=True)
            raise typer.Exit(1) from exc
        threshold = min_average
        label = f"average below {min_average}"
        if min_grade:
            rubric = load_rubric(rating_prompt)
            band = next(
                (item for item in rubric.grade_bands if item.label.lower() == min_grade.strip().lower()),
                None,
            )
            if band is None:
                known = ", ".join(item.label for item in rubric.grade_bands)
                typer.echo(f"Unknown grade '{min_grade}'. Known grades: {known}", err=True)
                raise typer.Exit(1)
            threshold = band.minimum
            label = f"grade below {band.label} (average < {threshold})"
        grade_slugs = {
            slug for slug, info in grades.items() if info["average"] < (threshold or 0.0)
        }
        if not quiet:
            _log(f"Grade gated to {len(grade_slugs)} post(s) with {label}.")

    slug_filter: set[str] | None = None
    if ratings and flagged_only:
        slug_filter = set(deslop_slugs or ())
    if grade_slugs is not None:
        slug_filter = grade_slugs if slug_filter is None else (slug_filter & grade_slugs)

    try:
        report = run_correct_blogs(
            input_dir=input_dir,
            output_dir=output_dir,
            reports_dir=reports_dir,
            prompt_path=prompt,
            apply=apply,
            limit=limit,
            slug=slug,
            workers=workers,
            rate_limit=rate_limit,
            max_attempts=max_attempts,
            timeout=timeout,
            search_timeout=search_timeout,
            fallback=use_fallback,
            verify_faq=verify_faq,
            fix_facts=fix_facts,
            fix_grammar=fix_grammar,
            fix_slop=fix_slop,
            fix_examos=fix_examos,
            seo=seo,
            deslop_slugs=deslop_slugs,
            slug_filter=slug_filter,
            review=review,
            review_threshold=review_threshold,
            rating_prompt=rating_prompt,
            faq_applies=faq_applies,
            max_searches=max_searches,
            on_progress=None if quiet else _progress,
            on_start=None if quiet else _start,
            on_retry=None if quiet else _retry,
            on_provider_switch=_provider_switch,
        )
    except KeyboardInterrupt:
        _log(
            f"\nInterrupted by user. Partial results ({reports_dir}\\correct-*.diff/.json) "
            "contain every correction finished so far."
        )
        os._exit(130)
    except FatalLLMError as exc:
        typer.echo(f"Fatal LLM error, aborting: {exc}", err=True)
        raise typer.Exit(1) from exc
    except (ValueError, FileNotFoundError) as exc:
        typer.echo(f"Correction failed: {exc}", err=True)
        raise typer.Exit(1) from exc

    elapsed = time.monotonic() - started
    mode = "applied" if apply else "dry-run"
    typer.echo(
        f"[correct-blogs] scanned={report.files_scanned} changed={report.files_changed} "
        f"failed={report.files_failed} flagged={report.files_flagged} "
        f"web-searches={report.searches_used} ({mode}, {_fmt_duration(elapsed)})"
    )
    if quiet:
        for change in report.changes:
            if change.error:
                typer.echo(f"    ! {change.slug}: {change.error}")
                continue
            label = "; ".join(change.notes) or "no change"
            typer.echo(f"    ~ {change.slug}: {label}")
    flagged = [change.slug for change in report.changes if change.review_flagged]
    if flagged:
        typer.echo(f"    flagged for human review ({len(flagged)}): {', '.join(flagged)}")
    typer.echo(f"Diff report: {report.extra.get('diff_report')}")
    typer.echo(f"JSON report: {report.extra.get('json_report')}")
    if report.extra.get("rating_report"):
        typer.echo(f"Post-correction rating: {report.extra.get('rating_report')}")
    if report.extra.get("human_review_report"):
        typer.echo(f"Human review: {report.extra.get('human_review_report')}")
    if apply:
        typer.echo(f"Corrected files: {output_dir}")


@app.command("fix-frontmatter")
def fix_frontmatter(
    input_dir: str = typer.Option(
        ..., "--input", "-i", help="Folder of content files whose frontmatter to validate."
    ),
    output_dir: str = typer.Option(
        "output_frontmatter", "--output", help="Where corrected files go in apply mode."
    ),
    reports_dir: str = typer.Option("reports", "--reports", help="Where reports go."),
    profiles: str = typer.Option(
        DEFAULT_MANIFEST_PATH, "--profiles", help="Content-type manifest."
    ),
    content_type: str | None = typer.Option(
        None, "--content-type", help="Override the content type (else inferred from the input folder)."
    ),
    prompt: str | None = typer.Option(
        None,
        "--prompt",
        help="Frontmatter schema prompt (Markdown tables or YAML frontmatter). Defaults to the content type's schema.",
    ),
    apply: bool = typer.Option(
        False, "--apply", help="Write corrected files to --output (never modifies --input)."
    ),
    limit: int | None = typer.Option(None, "--limit", help="Process only the first N files."),
    slug: str | None = typer.Option(None, "--slug", help="Process a single file slug."),
    order_file: str | None = typer.Option(
        None, "--order-file", help="JSON file mapping slug -> integer order value."
    ),
    only_order: bool = typer.Option(
        False, "--only-order", help="Only update the 'order' field from --order-file; change nothing else."
    ),
    exam_db: str | None = typer.Option(
        "exam_database.txt",
        "--exam-db",
        help="Exam database JSON (code + name) used to detect and fill examCodes. "
        "Pass an empty string to disable name detection.",
    ),
    seo: bool = typer.Option(
        True,
        "--seo/--no-seo",
        help="Rewrite title/seoTitle/seoDescription that are missing or out of range with the LLM.",
    ),
    seo_prompt: str | None = typer.Option(
        None,
        "--seo-prompt",
        help="SEO rewrite prompt file. Defaults to the content type's SEO prompt.",
    ),
    use_llm: bool = typer.Option(
        False, "--llm/--no-llm", help="Let Gemini infer values the deterministic rules cannot."
    ),
    workers: int = typer.Option(4, "--workers", "-w", help="Concurrent worker threads."),
    rate_limit: float = typer.Option(
        0.0, "--rate-limit", help="Max LLM requests per second (0 = unlimited)."
    ),
    max_attempts: int = typer.Option(
        5, "--max-attempts", help="Retries per LLM call on 429/5xx/timeouts."
    ),
    timeout: float = typer.Option(120.0, "--timeout", help="Per-request LLM timeout in seconds."),
    fallback: bool = typer.Option(
        True, "--fallback/--no-fallback", help="Switch to DeepSeek if Gemini times out."
    ),
    ping: bool = typer.Option(
        True, "--ping/--no-ping", help="Verify LLM connectivity before an --seo/--llm run."
    ),
    quiet: bool = typer.Option(
        False, "--quiet", "-q", help="Hide live progress; print a detailed summary at the end."
    ),
) -> None:
    """Frontmatter agent: enforce schema field order and values (frontmatter only)."""
    if only_order and not order_file:
        typer.echo("--only-order requires --order-file", err=True)
        raise typer.Exit(1)
    if apply and not output_dir:
        typer.echo("--apply requires --output <dir>", err=True)
        raise typer.Exit(1)

    _require_dir(input_dir, "--input")
    _require_optional_file(order_file, "--order-file")
    resolved = _resolve_profile(input_dir, content_type, profiles)
    _announce_profile(resolved, content_type)
    profile = resolved.profile if resolved else None
    if profile is not None and profile.frontmatter == "skip":
        typer.echo(
            f"content-type {resolved.content_type}: frontmatter is skipped for this type; nothing to do."
        )
        return
    prompt = prompt or (profile.frontmatter_prompt if profile else None) or DEFAULT_FRONTMATTER_PROMPT
    seo_prompt = seo_prompt or (profile.seo_prompt if profile else None) or DEFAULT_SEO_PROMPT
    _require_file(prompt, "--prompt")
    if seo and not only_order:
        _require_file(seo_prompt, "--seo-prompt")
    nested_preserve = profile.nested_preserve if profile else ()

    started = time.monotonic()
    log_lock = threading.Lock()

    def _log(message: str) -> None:
        with log_lock:
            print(message, file=sys.stderr, flush=True)

    def _start(index: int, count: int, slug: str) -> None:
        _log(f"  -> [{index}/{count}] checking {slug} ...")

    def _progress(index: int, count: int, outcome) -> None:
        elapsed = time.monotonic() - started
        rate = index / elapsed if elapsed > 0 else 0.0
        remaining = (count - index) / rate if rate > 0 else 0.0
        stamp = f"[{index:>{len(str(count))}}/{count}] {_fmt_duration(elapsed)} elapsed"
        if remaining > 0:
            stamp += f", ~{_fmt_duration(remaining)} left"
        if outcome.error:
            _log(f"{stamp}  FAILED {outcome.slug}: {outcome.error}")
            return
        changed = "changed" if outcome.result.changes else "no change"
        flags = len(outcome.result.review_items)
        _log(f"{stamp}  OK {outcome.slug} [{changed}{f', {flags} review' if flags else ''}]")

    def _provider_switch(provider: str, exc: Exception) -> None:
        _log(f"  ** Gemini {type(exc).__name__}: falling back to {provider} for this request.")

    if ping and (use_llm or seo) and not only_order:
        if not quiet:
            _log("Checking LLM connectivity ...")
        try:
            reply = ping_llm(timeout=min(timeout, 30.0))
        except Exception as exc:
            typer.echo(
                f"LLM connectivity check failed: {type(exc).__name__}: {exc}\n"
                "Rerun with --no-ping to skip this check, or --no-seo --no-llm for a rules-only run.",
                err=True,
            )
            raise typer.Exit(1) from exc
        if not quiet:
            _log(f"LLM OK: {reply[:80] or '(empty response)'}")

    try:
        report = run_fix_frontmatter(
            input_dir=input_dir,
            output_dir=output_dir,
            reports_dir=reports_dir,
            prompt_path=prompt,
            apply=apply,
            limit=limit,
            slug=slug,
            order_file=order_file,
            only_order=only_order,
            use_llm=use_llm,
            seo=seo,
            seo_prompt=seo_prompt,
            exam_db=exam_db,
            nested_preserve=nested_preserve,
            workers=workers,
            rate_limit=rate_limit,
            max_attempts=max_attempts,
            timeout=timeout,
            fallback=fallback,
            on_progress=None if quiet else _progress,
            on_start=None if quiet else _start,
            on_provider_switch=_provider_switch,
        )
    except KeyboardInterrupt:
        _log(
            f"\nInterrupted by user. Partial results ({reports_dir}\\frontmatter-*.json/.md/.diff) "
            "contain every file finished so far."
        )
        os._exit(130)
    except FatalLLMError as exc:
        typer.echo(f"Fatal LLM error, aborting: {exc}", err=True)
        raise typer.Exit(1) from exc
    except (ValueError, TypeError, FileNotFoundError) as exc:
        typer.echo(f"Frontmatter run failed: {exc}", err=True)
        raise typer.Exit(1) from exc

    elapsed = time.monotonic() - started
    mode = "applied" if apply else "dry-run"
    typer.echo(
        f"[fix-frontmatter] scanned={report.files_scanned} changed={report.files_changed} "
        f"flagged={report.files_flagged} failed={report.files_failed} "
        f"({mode}, {'only-order' if only_order else 'full'}, {_fmt_duration(elapsed)})"
    )
    if report.extra.get("missing_order_slugs"):
        missing = ", ".join(report.extra["missing_order_slugs"])
        typer.echo(f"    order file slugs with no content file ({len(report.extra['missing_order_slugs'])}): {missing}")
    if quiet:
        for change in report.changes:
            if change.error:
                typer.echo(f"    ! {change.slug}: {change.error}")
                continue
            label = "; ".join(change.notes) or ("order only" if only_order else "no change")
            flags = f" | review: {len(change.review_items)}" if change.review_items else ""
            typer.echo(f"    ~ {change.slug}: {label}{flags}")
    typer.echo(f"Changelog: {report.extra.get('markdown_report')}")
    typer.echo(f"JSON report: {report.extra.get('json_report')}")
    if apply:
        typer.echo(f"Corrected files: {output_dir}")


@app.command("check-facts")
def check_facts(
    input_dir: str = typer.Option(
        ..., "--input", "-i", help="Folder of content files to fact-check (required)."
    ),
    output_dir: str = typer.Option(
        "output_factchecked", "--output", help="Where corrected files go in apply mode."
    ),
    reports_dir: str = typer.Option("reports", "--reports", help="Where reports go."),
    profiles: str = typer.Option(
        DEFAULT_MANIFEST_PATH, "--profiles", help="Content-type manifest."
    ),
    content_type: str | None = typer.Option(
        None, "--content-type", help="Override the content type (else inferred from the input folder)."
    ),
    facts_dir: str = typer.Option("facts", "--facts", help="Folder of verified fact JSON files."),
    prompt: str | None = typer.Option(
        None,
        "--prompt",
        help="Fact-check prompt file (content-type guidance for the optional LLM pass). "
        "Defaults to the content type's prompt.",
    ),
    apply: bool = typer.Option(
        False, "--apply", help="Write corrected files to --output (never modifies --input)."
    ),
    fix: bool = typer.Option(
        True, "--fix/--no-fix", help="Apply the safe deterministic corrections (names, values, dates)."
    ),
    limit: int | None = typer.Option(None, "--limit", help="Check only the first N files."),
    slug: str | None = typer.Option(None, "--slug", help="Check a single file slug."),
    use_llm: bool = typer.Option(
        False, "--llm/--no-llm", help="Also run the LLM pass (prompt + fact sheet)."
    ),
    workers: int = typer.Option(4, "--workers", "-w", help="Concurrent worker threads."),
    rate_limit: float = typer.Option(
        0.0, "--rate-limit", help="Max LLM requests per second (0 = unlimited)."
    ),
    max_attempts: int = typer.Option(
        5, "--max-attempts", help="Retries per LLM call on 429/5xx/timeouts."
    ),
    timeout: float = typer.Option(120.0, "--timeout", help="Per-request LLM timeout in seconds."),
    fallback: bool = typer.Option(
        True, "--fallback/--no-fallback", help="Switch to DeepSeek if Gemini times out."
    ),
    ping: bool = typer.Option(True, "--ping/--no-ping", help="Verify LLM connectivity before an --llm run."),
    quiet: bool = typer.Option(
        False, "--quiet", "-q", help="Hide live progress; print a detailed summary at the end."
    ),
) -> None:
    """Fact checker: validate content against the verified exam fact files."""
    _require_dir(input_dir, "--input")
    _require_dir(facts_dir, "--facts")
    resolved = _resolve_profile(input_dir, content_type, profiles)
    _announce_profile(resolved, content_type)
    profile = resolved.profile if resolved else None
    prompt = prompt or (profile.facts_prompt if profile else None) or DEFAULT_FACT_PROMPT
    if use_llm:
        _require_file(prompt, "--prompt")
    exam_binding = tuple(profile.exam_binding) if profile and profile.exam_binding else None
    vendor_contamination = profile.vendor_contamination if profile else True
    started = time.monotonic()
    log_lock = threading.Lock()

    def _log(message: str) -> None:
        with log_lock:
            print(message, file=sys.stderr, flush=True)

    def _start(index: int, count: int, slug: str) -> None:
        _log(f"  -> [{index}/{count}] fact-checking {slug} ...")

    def _progress(index: int, count: int, outcome) -> None:
        elapsed = time.monotonic() - started
        rate = index / elapsed if elapsed > 0 else 0.0
        remaining = (count - index) / rate if rate > 0 else 0.0
        stamp = f"[{index:>{len(str(count))}}/{count}] {_fmt_duration(elapsed)} elapsed"
        if remaining > 0:
            stamp += f", ~{_fmt_duration(remaining)} left"
        if outcome.error:
            _log(f"{stamp}  FAILED {outcome.slug}: {outcome.error}")
            return
        result = outcome.result
        if not result.exams:
            return
        _log(
            f"{stamp}  OK {outcome.slug} [{', '.join(result.exams)}] "
            f"{sum(1 for f in result.findings if f['applied'])} fixed, "
            f"{sum(1 for f in result.findings if not f['applied'])} flagged"
        )

    def _provider_switch(provider: str, exc: Exception) -> None:
        _log(f"  ** Gemini {type(exc).__name__}: falling back to {provider} for this request.")

    if use_llm and ping:
        if not quiet:
            _log("Checking LLM connectivity ...")
        try:
            reply = ping_llm(timeout=min(timeout, 30.0))
        except Exception as exc:
            typer.echo(
                f"LLM connectivity check failed: {type(exc).__name__}: {exc}\n"
                "Rerun with --no-ping, or omit --llm for a rules-only run.",
                err=True,
            )
            raise typer.Exit(1) from exc
        if not quiet:
            _log(f"LLM OK: {reply[:80] or '(empty response)'}")

    try:
        report = run_check_facts(
            input_dir=input_dir,
            output_dir=output_dir,
            reports_dir=reports_dir,
            facts_dir=facts_dir,
            prompt_path=prompt,
            apply=apply,
            fix=fix,
            limit=limit,
            slug=slug,
            use_llm=use_llm,
            workers=workers,
            rate_limit=rate_limit,
            max_attempts=max_attempts,
            timeout=timeout,
            fallback=fallback,
            exam_binding=exam_binding,
            vendor_contamination=vendor_contamination,
            on_progress=None if quiet else _progress,
            on_start=None if quiet else _start,
            on_provider_switch=_provider_switch,
        )
    except KeyboardInterrupt:
        _log(
            f"\nInterrupted by user. Partial results ({reports_dir}\\factcheck-*.json/.md/.diff) "
            "contain every file finished so far."
        )
        os._exit(130)
    except FatalLLMError as exc:
        typer.echo(f"Fatal LLM error, aborting: {exc}", err=True)
        raise typer.Exit(1) from exc
    except (ValueError, TypeError, FileNotFoundError) as exc:
        typer.echo(f"Fact check failed: {exc}", err=True)
        raise typer.Exit(1) from exc

    elapsed = time.monotonic() - started
    mode = "applied" if apply else "dry-run"
    typer.echo(
        f"[check-facts] scanned={report.files_scanned} with-exam={report.files_with_exam} "
        f"changed={report.files_changed} flagged={report.files_flagged} "
        f"fixed={report.total_applied} unresolved={report.total_unresolved} "
        f"failed={report.files_failed} ({mode}, {_fmt_duration(elapsed)})"
    )
    if quiet:
        for change in report.changes:
            if change.error:
                typer.echo(f"    ! {change.slug}: {change.error}")
                continue
            if not change.exams:
                continue
            typer.echo(
                f"    ~ {change.slug} [{', '.join(change.exams)}]: "
                f"{change.applied} fixed, {change.unresolved} flagged"
            )
    typer.echo(f"Human review: {report.extra.get('markdown_report')}")
    typer.echo(f"Diff report: {report.extra.get('diff_report')}")
    typer.echo(f"JSON report: {report.extra.get('json_report')}")
    if apply:
        typer.echo(f"Corrected files: {output_dir}")


def _fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


if __name__ == "__main__":
    app()
