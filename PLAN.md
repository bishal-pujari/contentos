# contentos — Project Plan & Status

> Resume note: this file is the single source of truth for *where we are*.
> `AGENTS.md` describes the architecture per agent; `README.md` is the user-facing
> command reference. Update all three when a milestone lands.

## Goal
Agent-based content pipeline for SEO: produce content that ranks and converts
impressions into clicks. Content flows `blog/` (source, never mutated) →
`output/` (stripped/interlinked) → `output_corrected/` (repaired), with a rating
audit at every stage.

## Status at a glance
- **Agents built and working:** strip-links, interlink, review-interlinks, rate-blogs,
  correct-blogs, fix-frontmatter, **check-facts**. A writer is planned.
- **Multi-content-type (Phase 1) landed:** every agent is profile-driven via
  `prompts/content_types.json`. `--content-type` (or the folder) selects the rubric,
  correction, frontmatter and SEO prompts, the grounding mode and the FAQ/strip-links
  behaviour for **blogs, study-plans, roadmaps and exam-faq**. See
  `CONTENT_TYPES_PLAN.md`; the learn family is Phase 2.
- **Tests:** `pytest` **214 passing**; `ruff` clean.
- **Corpus:** 81 content files in `blog/`; `output/` holds the stripped set plus
  `output/interlinks.json`.
- **Corrector outputs three reports per run:** `correct-*` (the diff), a **rating report**
  for the post-correction review (`rating-*.md`/`.json`), and `human-review-*.md` for any
  post that stayed below the A threshold.
- **LLM backend:** Vertex AI, project `marketos-504718`, region **`asia-south1`**
  (a zone like `asia-south1-a` is auto-reduced to the region), authenticated with
  ADC. Developer API + DeepSeek fallback also supported.

## Build order (done)
1. ✅ Scaffold (pyproject, package layout, LLM config, `.env.example`).
2. ✅ Agent 1 link stripper + pipeline + reports + CLI.
3. ✅ Agent 2 interlink (LLM selects, code enforces guardrails).
4. ✅ Agent 3 interlink reviewer (deterministic repair + optional LLM redo).
5. ✅ Agent 4 blog rater (external rubric, A+..F, grounded fact verification).
6. ✅ Agent 5 blog corrector (deterministic + validated LLM repairs).
7. ✅ Frontmatter validator/corrector (external schema prompt, deterministic, order file).
8. ✅ Fact checker (verified fact files, deterministic checks, optional LLM pass).
9. ⬜ Writer (draft / genuinely expand thin content).

## Key decisions (log)
- **Python + LangChain**, Typer CLI, dataclasses (not Pydantic).
- **Deterministic rules first, LLM second.** The LLM only *proposes* values (JSON);
  Python validates and applies them, so a bad response cannot break output.
- **Safety-first:** `blog/` is never edited in place. `--apply` writes to a separate
  `--output`; dry-run (diff) is the default everywhere.
- **Frontmatter:** the stripper/body agents never touch it; the corrector edits only
  the specific SEO keys (`title`/`seoTitle`/`seoDescription`) via targeted line
  replacement, never a full re-dump.
- **Vertex AI** for the $300 trial credit (grounding billed to the project);
  Developer API kept for later. Search capped per run (`MAX_WEB_SEARCHES`, default
  1500) via `--max-searches`.
- **Fallback is non-sticky:** a Gemini timeout falls back to DeepSeek for that one
  call only; the next call starts with Gemini again.
- **Auth/credential errors are fatal:** retry/fallback cannot help, so the run
  aborts instead of failing every post.
- **Reports are atomic + microsecond-unique** (temp file + rename), so two runs can
  never collide on one filename and corrupt each other (an earlier rating report was
  lost this way; fixed).
- **Length/depth:** word count is a soft guide, not a gate. The corrector never pads
  content (that creates fluff); genuinely thin content is a writer job.
- **Facts are best-effort:** grounded `find -> replace` fixes require an official
  source and exact, word-boundary text, but can still be wrong — always review.

## LLM / runtime facts
- Retries: 429/408/409/5xx/timeouts, exponential backoff + jitter, `Retry-After`,
  `--max-attempts` (default 5), `--timeout` (default 120s).
- `--ping` (default on) verifies connectivity before a batch.
- Concurrency `--workers` (default 4) + optional `--rate-limit` (req/s).
- Live progress on **stderr**; `--quiet` prints a summary. Ctrl+C flushes partial
  reports (rating/correction write after every completed file) and exits.
- Every phrase/fact rewrite is rejected when its length ratio leaves the band
  (grammar ≤3x, de-slop ≤1.8x, facts ≤1.5x, never below ~0.5x) — the guard against
  rambling/hallucinated rewrites.

## Recent work (last session)
- **SEO ownership moved to `fix-frontmatter`.** `correct-blogs` no longer rewrites
  `title`/`seoTitle`/`seoDescription` by default (`--seo/--no-seo`, default **off**); it
  still fixes FAQ headings, dangling endings, FAQ sections, facts, grammar, slop and
  ExamOS callouts. Pass `--seo` to re-enable the old corrector SEO pass. This removes the
  double-rewrite when the pipeline runs `correct-blogs` then `fix-frontmatter --seo`.
- **Grade gating for `correct-blogs`:** `--min-grade A-` / `--min-average 4.0` (with
  `--ratings`) restrict the run to posts below that score; `--flagged-only` remains the
  AI-slop gate.
- **Added the fact checker** (`contentos check-facts`, `agents/factchecker.py`). Links each
  file to its exam by frontmatter `examCodes` (or detection from the `facts/` registry) and
  validates it against the verified fact JSON. Deterministic checks cover service renames
  (auto-applied, unless in a rename discussion / conflicting / not primary), retired,
  retiring and out-of-scope services (flagged), price, passing score, duration, question
  counts, predecessor retirement date, GA date, last day to take and exam status. Value
  fixes require a single primary active exam with an exact fact and a clearly-tied claim;
  everything else is flagged for human review. Optional `--llm` pass reads
  `prompts/examos-content-facts.md` and proposes sourced `find -> replace` fixes validated
  by code. Writes `reports/factcheck-*.{diff,json,md}`; `--apply` writes to
  `output_factchecked/`. **Domain-weight math** (per-domain and grouped-claim checks) and
  **cross-cloud vendor contamination** are included, and the prompt defines an exact JSON
  output schema. Corpus dry-run: 67/81 files linked to an exam, 6 files changed, 12 flagged.
  Fixes: conservative name aliases now match common cert names ("Cloud Practitioner",
  "Solutions Architect Professional") in the body; service `renames` are no longer treated
  as exam aliases (a Google service rename could falsely link GCP-PCNE to an AWS post);
  exam slugs inside markdown URLs are ignored; and value fixes are attribution-based so a
  comparison post cannot rewrite one exam's numbers with another's. 19 new tests; 166
  passing, ruff clean.
- **Added the frontmatter validator/corrector** (`contentos fix-frontmatter`,
  `agents/frontmatter.py`). The schema is external: the prompt's Markdown tables (or
  optional YAML frontmatter) define field order, required flags, fixed values, approved
  authors/categories/exam codes and aliases. Deterministic by default — reorders fields,
  renames aliases, de-duplicates, quotes strings, maps/drops unapproved categories,
  fixes authors, infers missing required fields (flagged), removes study-plan fields from
  blogs, and enforces `slug` = filename. `--order-file` sets `order` from a slug → integer
  map; `--only-order` updates just that line. Optional `--llm` (Vertex Gemini) fills
  values the rules cannot. Writes `reports/frontmatter-*.{diff,json,md}`; `--apply` writes
  to `output_frontmatter/`. The body is never touched. 24 tests; 146 passing, ruff clean.
- **Frontmatter: LLM SEO rewrite + examCodes detection.** `title`/`seoTitle`/
  `seoDescription` that are missing or out of range are rewritten with a dedicated prompt
  (`prompts/examos_blog_seo_rewritre.md`, `--seo`/`--seo-prompt`, default on); code
  validates every candidate (length, no `!`/em dash, no filler, `seoTitle` ≠ `title`) and
  retries once, so it never trims and never writes an out-of-range value. Every valid exam
  the content references is detected and written to `examCodes` — explicit codes plus exam
  names from `exam_database.txt` (`--exam-db`) and curated aliases (`SAFe Agilist` →
  `SA_6`, `Google Professional Cloud Architect` → `GCP-PCA`). Broad aliases are used in
  metadata only and conservative aliases in the body, so phrases like `Security + Data`
  are not mistaken for `Security+`. On the corpus: examCodes filled in 69/81 posts and a
  full SEO rewrite run cuts flagged files from 70 to 19.
- Hardened the report writer (atomic writes, unique filenames) after a corrupt
  `rating-*.json`.
- Made the corrector resilient and richer:
  - regenerates a FAQ that has fewer than 4 questions (not just a missing one),
    bounding the section at the next H2/`---` so the closing CTA survives;
  - grounded factual `find -> replace` pass;
  - grammar pass (fragments/run-ons) and AI-slop pass (intensifiers, filler,
    scare quotes), both with exact-match + length-band guards;
  - `--ratings <report>` gates de-slop to posts scoring below `--slop-threshold`
    (default 3) on `voice_ai_slop`; `--flagged-only` restricts the run to those files.
- Tightened the rating rubric:
  - factual correctness: a *confirmed* error is a hard fail (score 1-2);
  - length/depth: judge substance/density, word count only a soft guide, penalise
    padding too;
  - voice/AI-slop: penalise every tell, explicitly including empty intensifiers
    (`actually`, `genuinely`, ...), filler openers and scare quotes (quotes only for
    sources/defined terms);
  - internal linking: no links is the expected (post-stripper) state → score 5;
  - FAQ: heading consistency fixed; fewer than 4 questions caps the criterion at 2.
- Ran with the stricter rubric (no web search) → **4 posts scored 2.0 on AI slop**:
  `ai-103-most-misunderstood-concepts`,
  `ai-200-study-guide-vector-search-managed-identity-observability-containers`,
  `ai-empowered-safe-agilist`, `is-ai-901-worth-it`. A gated dry-run
  (`--ratings ... --flagged-only`) corrected only those 4 (all within length bands).
- **Aligned the two prompts.** `prompts/examos-blog-corrections.md` and
  `prompts/examos-blog-rating.md` now share the same numbers/rules: SEO lengths
  (title 50-65, seoTitle 55-65/max 70, seoDescription 140-160), the required FAQ
  heading `## Frequently Asked Questions (FAQ)`, the full voice/AI-slop tell list
  (incl. scare quotes), and the allowed ExamOS features. The correction prompt also
  gained a LENGTH rule (never pad) and an EXAMOS RULES section.
- **Corrector: ExamOS pass** (`--fix-examos`, default on) — fixes invented ExamOS
  features / wrong booking triggers using only the real features.
- **Corrector: post-correction review** (`--review`, default on) — every corrected file
  is re-rated with the rating rubric and flagged (`review_grade`/`review_average`/
  `review_flagged`) when its average is below `--review-threshold` (default 4.3 = A);
  the run summary lists the flagged slugs. New `parse_document` helper re-parses the
  in-memory corrected text for the review.
- Tests now **137 passing**, ruff clean.
- **Corrector reporting split into three files.** A correction run now writes:
  `reports/correct-*.diff` + `.json` (the changes), a normal **rating report**
  (`reports/rating-*.md`/`.json`) for the post-correction review, and — when any post is
  below the threshold — a focused **`reports/human-review-*.md`** that lists each flagged
  post with its specific issues (weaknesses, factual issues, low-scoring criteria), what
  the corrector already changed, warnings and sources.
- **Review is grounded.** A search model is built whenever `--review` is on (even with
  `--no-verify-faq`), because an ungrounded review falsely claimed the AI-103/AI-200
  exams were fictional. Review consumes the shared `--max-searches` budget.

## How to resume
```bash
pip install -e '.[dev]'
# 1. confirm Vertex auth + a live ping
contentos rate-blogs --input output --limit 1        # runs --ping first
# 2. re-rate for a clean, valid report (writer-crash bug is fixed)
contentos rate-blogs --input output --reports reports
# 3. review corrections as a diff, then apply to a new folder
contentos correct-blogs --input output --reports reports
contentos correct-blogs --input output --apply       # -> output_corrected/
#    (writes reports/correct-* + a rating report + reports/human-review-*.md)
# 4. normalise frontmatter against the schema (rules-only, no key needed)
contentos fix-frontmatter --input output --reports reports        # dry-run + changelog
contentos fix-frontmatter --input output --reports reports --apply   # -> output_frontmatter/
contentos fix-frontmatter --input output --order-file prompts/examos-blog-order.json --only-order --apply
```

## Next session (start here)
1. **Run the full corrector with review** and inspect the three outputs:
   `contentos correct-blogs --input output --reports reports` (dry-run first), then
   `--apply`. Confirm the post-correction `reports/rating-*.json` is valid and read
   `reports/human-review-*.md` to see what still needs a human.
2. **Triage the human-review file.** Decide for each flagged post whether it is a real
   issue (fix manually or via a targeted `--slug` run) or a review false positive.
3. **Full grounded rating** (`rate-blogs` with facts on) to get the canonical baseline
   before/after corrections; compare with the corrector's review rating.
4. Keep the pending items below in view; item 3 (fact-fix safety) and item 6
   (`.env` housekeeping) are quick wins.

## Pending tasks / open items
1. **Apply corrections and review diffs.** Produce `output_corrected/` for the 4
   slop-flagged files first (minimal), then decide on a full pass.
2. **Full rating run with search.** The last full run predates the report-writer fix
   and the stricter rubric; the most recent rating skipped web search to be cheap.
   Re-run with facts on to get a trustworthy report.
3. **Fact-fix safety.** Keep `--fix-facts` on but always review sources; a live run
   proposed a debatable `SAP-C03 -> SAP-C02`. Consider defaulting fact fixes to
   proposal-only if it proves noisy.
4. **Length/depth.** Do not auto-expand. Use the writer for genuinely thin
   posts; the rater now scores density rather than raw word count.
5. **Writer** — draft/regenerate and legitimately deepen content.
6. **`.env` housekeeping:** set `GOOGLE_CLOUD_REGION=asia-south1` (drop the `-a`
   zone); the file was saved in a non-UTF-8 encoding (a comment shows mojibake) —
   re-save as UTF-8.
7. **Stray file:** `output/security-plus-vs-cc-vs-sc-900-which-first.txt` is picked
   up as content; decide whether to keep `.txt` support or convert to `.mdx`.
8. **Billing check:** confirm `marketos-504718` is linked to the free-trial billing
   account, not the paid one (manual Console check; Billing API is disabled on the
   project).
9. **Optional gating:** `--min-grade A-` / `--min-average 4.0` now restrict the corrector
   to posts below a grade (alongside the de-slop `--slop-threshold` / `--flagged-only`).
   Remaining: per-criterion gating (e.g. run SEO only where `seo_quality` is low, grammar
   only where `sentence_grammar` is low) if edits should follow the rubric more granularly.
10. **Review threshold tuning:** the post-correction review defaults to `4.3` (A) and is
    grounded (search is built whenever review is on, counted against `--max-searches`).
    If a full run flags many posts, inspect whether the flags are real before widening
    the threshold.
11. **Frontmatter first pass.** Run `contentos fix-frontmatter --input blog --reports reports`
    and read `reports/frontmatter-*.md`. Field order, SEO metadata (LLM rewrite, `--seo`
    default on) and `examCodes` (detected) are fixed automatically. A full run leaves ~19
    files flagged: mapped or dropped category tags (`pmp` → `project-management`, `ml` →
    `mlops`, `engineering` / `administration` / `examos` dropped), one unapproved author
    (`Praveen Sharma` → `Pradeep Sharma`), 3 files missing `author` / 6 missing `date`
    (inferred), plus the rare SEO field the rewriter still could not fit. Confirm the
    inferred authors and dropped categories before `--apply`; use `--no-seo` for a
    rules-only pass with no key.
12. **Fact-check first pass.** Run `contentos check-facts --input blog --reports reports`
    and read `reports/factcheck-*.md`. A dry-run links 69/81 files to an exam, applies ~9
    corrections and flags ~32 items. Review the applied service renames and the flagged
    retired/out-of-scope services, prices and passing scores before `--apply`; add
    `--llm` for the semantic pass. Note the fact files are authoritative — if a value
    looks wrong, fix the fact file, not the agent.

## Near-term definition of done
A clean `rate-blogs` report (valid JSON, stricter rubric) plus an `output_corrected/`
set that improves the low-scoring criteria without introducing drift, verified by a
re-rate and a reviewed diff.
