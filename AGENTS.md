# AGENTS.md — contentos technical architecture

## Overview
`contentos` is an agent-based content pipeline for SEO. Goal: produce content that
ranks high in search results and turns impressions into clicks. Agents: **strip-links
(1), interlink (2), review-interlinks (3), rate-blogs (4), correct-blogs (5),
fix-frontmatter (6), check-facts (7)** — a writer is planned. Each is a standalone command
on a shared pipeline skeleton (load → transform → diff report → optional write). See
`PLAN.md` for status/roadmap.

## Stack
- **Language:** Python 3.11+ (developed on 3.14)
- **Frameworks:** LangChain (chat models); dataclasses for schema.
- **LLM provider (default):** Google Gemini, with optional Google Search grounding
  for fact-checking. Backends: **Vertex AI** (`aiplatform.googleapis.com`, via
  `google-cloud-aiplatform`, billed to a GCP project) or the **Gemini Developer
  API** (API key); auto-selected from `GEMINI_BACKEND`/`GOOGLE_CLOUD_PROJECT`.
  Fallback: the DeepSeek API (per-call, non-sticky).
- **CLI:** Typer; **reports/output:** Markdown, JSON, unified diff.

## Repo layout
```
blog/                       # source content (MDX/markdown), INPUT — never mutated in place
input/                      # interlinks.json, content_inventory.json, guidelines
facts/                      # verified per-exam fact JSON (source of truth for check-facts)
prompts/                    # external rubric prompts (YAML frontmatter + body)
src/contentos/
  agents/link_stripper.py   # Agent 1 — deterministic link stripping
  agents/interlinker.py     # Agent 2 — LLM link selection + code guardrails
  agents/interlink_reviewer.py  # Reviewer — deterministic repair of interlinks.json
  agents/rater.py           # Agent 4 — rubric grading A+ to F
  agents/corrector.py       # Agent 5 — deterministic + validated LLM repairs
  agents/frontmatter.py     # Agent 6 — frontmatter schema validator/corrector
  agents/factchecker.py     # Agent 7 — verify content against the fact files
  core/loaders.py           # frontmatter-safe load, file discovery
  core/inventory.py         # content_inventory.json load + lookup helpers
  core/pipeline.py          # runner: load → agent → diff → write
  core/reporting.py         # unified diff + JSON/Markdown reports (atomic writes)
  core/llm.py               # Gemini + DeepSeek chat model factory
  core/protected.py         # fenced/inline-code + URL protection (shared by agents)
  core/profiles.py          # content-type manifest + input-folder resolution
  cli.py                    # typer commands
prompts/content_types.json  # content-type manifest (blog, study-plan, roadmap, exam-faq, learn)
output/                     # cleaned files / generated link mapping (apply mode)
reports/                    # per-run .json + .diff
```

## Key rules
- **Never mutate `blog/` in place.** Apply mode writes to `--output`.
- **Never touch frontmatter** in the body agents. The stripper splits YAML
  frontmatter off and only transforms the body; `ContentDocument` reassembles it
  verbatim. The *corrector* may edit only the SEO keys (`title` / `seoTitle` /
  `seoDescription`) via targeted per-line replacement, never a full YAML re-dump.
  The *frontmatter agent* is one exception: it re-emits the frontmatter in the
  schema order, but reassembles the body byte-for-byte unchanged. The *stripper* is
  the other: it removes standalone `<...Embed/>` component lines from nested YAML
  (roadmap `steps`/`milestones`) per-line, preserving every other frontmatter line.
- **The LLM only proposes; code applies.** The stripper's LLM pass (if any) may only
  *select*; the interlinker's LLM only *selects*; the corrector's LLM returns JSON
  that Python validates. Every phrase/fact rewrite must pass exact-match and a
  before/after length band before it is written, so content cannot drift.
- **Reports are atomic and microsecond-unique** (temp file + rename), so two
  concurrent runs can never collide on one filename and corrupt each other.

## Content types (profiles) — Phase 1
Every agent is content-type aware via `prompts/content_types.json` (see
`CONTENT_TYPES_PLAN.md`). Pass `--content-type <name>` or let the leaf folder infer it
(`blogs`, `study-plans`, `roadmaps`, `exam-faq`, and later `concept`/`eli10`/`blueprint`).

- The profile selects the per-type rating / correction / frontmatter / SEO prompt and the
  grounding mode: `ground: always` (study-plan, exam-faq) forces the rater's first pass to
  be search-grounded; `auto` (blog, roadmap) keeps the exam-specific heuristic.
- `faq.applies:false` (roadmap, exam-faq) means the corrector never adds or normalises a FAQ
  section; exam-faq is skipped entirely by `fix-frontmatter` (`frontmatter: skip`) and
  `strip-links` (`strip_links:false`).
- Roadmaps round-trip their nested YAML (`skills`, `certificationPath`, `milestones`,
  `steps`, …) verbatim through `nested_preserve`. Study plans use the singular `examCode`
  and a string `passingScore`.
- `check-facts` binds the fact file from `examCode`/`examCodes`/`exam_id`, the filename slug
  (exam-faq) or the learn exam folder; `vendor_contamination:false` disables the cross-cloud
  check for roadmaps.
- `core/protected.py` guards every replacement (corrector + fact checker): fenced blocks,
  inline code and URLs are never matched or rewritten.

## Agent 1: link stripper
Deterministic, regex-based, in order:

1. **Embed components** — remove `<PracticeExamEmbed examSlug="..."/>` self-closing
   component lines, including markdown-bulleted/indented ones and those nested in the
   roadmap frontmatter YAML (`strip_frontmatter_embeds`); every other frontmatter line
   is preserved.
2. **Label + link blocks** — a header label line (`Related Exam(s):`, `Further
   Reading:` … see `LABEL_LINE_RE`) followed by link-only lines → remove the whole block.
3. **Standalone link lines** — a line that is *only* a markdown link → remove line.
4. **Inline links** — `[anchor](url)` → keep the `anchor` text as plain prose.
5. **Bare URLs** — remove just the URL token so nearby sentences stay intact (no broken
   prose). This covers both `https://…` and root-relative paths such as `/exams/…` (the
   leftover `(/exams/…)` from an anchor that was already stripped). Fenced code blocks and
   inline `` `code` `` are protected by `core/protected.py`, so a link or path inside a
   snippet is never touched.
6. **Cleanup** — drop orphaned trailing horizontal rules; collapse 3+ blank lines to 1.

### Output
- Dry-run creates a unified diff (`reports/strip_links-*.diff`) + JSON summary
  (`reports/strip_links-*.json`) for review.
- `--apply` writes cleaned files under `--output` preserving relative paths.

## Agent 2: interlink
For each source article, the LLM reads the frontmatter + body and the full
`content_inventory.json`, identifies the primary exam code / topic / audience,
scores every inventory item (100 exact exam → 40 same topic; nothing below 40 is
linked), and selects links with anchor text and a justified position. Python then
enforces the hard rules — the LLM only *selects*, it never writes content:

- the source's frontmatter `examCodes` drive quiz and exam links: at least the first
  code's quiz is placed at an inline slot, one quiz per code up to three (quizzes may
  sit at `20%`/`50%`/`75%`, preferring `50%`/`75%`/`20%`); the exam page(s) go in
  `related` (lower priority). With `examCodes` present, only those exams' quizzes may
  be linked;
- one link per inline position (`20%`, `50%`, `75%`); `100%` is coerced to `related`;
- `top` required, score 80+, never `exam`/`quiz` — it prefers a study plan / roadmap for
  the primary exam when one exists; `related` holds 3-9 items;
- anchors ≤7 words, unique, no banned phrases / em dashes / exclamation marks,
  first letter capitalised (sentence case);
- no self-links. When no `top` scores 80+, the best foundational roadmap/study-plan
  is used and the source is flagged.

### Execution
- Sources are processed concurrently (`--workers`, default 4) through a thread
  pool; an optional client-side rate limiter (`--rate-limit`, requests/sec) spaces
  out calls so 429s are avoided in the first place.
- Every LLM call retries on 429/408/409/5xx/timeouts with exponential backoff and
  jitter, honouring `Retry-After` (`--max-attempts`, default 5). A per-request
  `--timeout` (default 120s) stops hung calls. Per-source failures are captured in
  the report and never abort the run.
- A connectivity ping (`--ping`/`--no-ping`, default on) verifies key/model/endpoint
  before the batch starts, so bad config fails in seconds.
- If a Gemini call times out, that single call is retried against the DeepSeek API
  (`DEEPSEEK_API_KEY`, `--fallback`/`--no-fallback`, default on) and the fallback
  is **not sticky** — the next call (and the next worker) starts with Gemini again.
  The per-call fallback is logged. Auth/credential errors are **fatal**: they abort
  the whole run immediately instead of retrying or falling back.
- Live status streams to **stderr**: `reviewing <slug>` on start, `retry n/m` on
  backoff, then `OK/FAILED <slug> ...` with count, elapsed and ETA. `--quiet` hides
  it and prints a detailed summary instead. Redirect with `2> progress.log` to
  monitor a long run. Ctrl+C exits cleanly without joining in-flight calls.

### Output
- Dry-run writes an analysis report (`reports/interlink-*.json`) with per-source
  exam/topic/audience, links and guardrail warnings.
- `--apply` merges the mapping into `--output` (default `output/interlinks.json`),
  keyed by source slug, preserving unrelated keys.

## Agent 3: interlink reviewer
Deterministic repair pass over an existing `interlinks.json`, plus an optional
targeted LLM redo. Runs without an LLM for the repair rules; the LLM is only
called for the flagged sources.

1. **Incomplete links** — an entry missing `slug`, `text`, `position` or `type` is
   repaired where possible (type inferred from the inventory, anchor derived from the
   target's seoTitle, a missing/unknown position defaulted to `related`) and dropped
   when it cannot be (no slug, unresolvable type, no anchor).
2. **Anchor text** — every anchor is sentence-cased (first letter capital) and
   case-insensitive duplicates are dropped.
3. **Reciprocal inline links** — when A links to B inline and B links back to A
   inline, the weaker direction is dropped. The original score is not persisted,
   so a deterministic proxy ranks each direction by exam-code relevance, position
   prominence and target type.
4. **100% links** — end-of-article links are promoted to `related` (the position
   duplicates the Related Reading section). The interlink prompt no longer emits
   `100%`, and `finalize_links` coerces any stray `100%` to `related`.
5. **Misplaced quizzes** — a quiz found at `top` or `related` is moved to a free
   `50%`/`75%` slot, or dropped when both are taken.
6. **Thin Related sections** — sources with exactly one `related` item
   (`--single-related`, default 1) are re-analysed with the LLM and only those
   entries in the mapping are replaced (`--redo`/`--no-redo`, default on).

### Output
- Always writes a review report (`reports/review-*.json`) listing every repair
  (incomplete link, anchor case), drop, promotion, quiz move and redo result.
- `--apply` writes the repaired mapping to `--output` (defaults to `--mapping`).

## Blog rating agent
Read-only audit of every content file under an explicit `--input` folder. The LLM
judges and scores all twelve rubric criteria (factual correctness, opening, depth,
scannability, FAQ, SEO, ExamOS callouts, voice/AI-slop, links, value, structure,
grammar) from 1-5; it returns JSON per post. Python then clamps the scores, computes
the weighted total (max 21.5) and the letter grade (A+ to F), and renders the
markdown report block, so the table, total and grade always agree.

**Rubrics are external** (`prompts/*.md`) so the agent is reusable across projects.
Each prompt file is Markdown with YAML frontmatter defining `name`, `criteria`
(`key`/`label`/`weight`) and `grade_bands` (`min`/`label`); the body is the system
prompt. Code auto-appends the JSON output contract from the criteria keys, so a new
rubric only needs its criteria descriptions. Select it with `--prompt` (default
`prompts/examos-blog-rating.md`).

- Runs concurrently (`--workers`), with optional `--rate-limit`, per-call retries
  (`--max-attempts`), `--timeout`, connectivity `--ping` and the DeepSeek
  `--fallback` exactly like the interlink agent.
- **Fact verification** (`--verify-facts/--no-verify-facts`, default on): posts that
  target a specific exam/certification — study guides, "is it hard", "is it worth
  it", comparisons, or any slug/title naming an exam or cert — are rated with
  Gemini's Google Search grounding on the first pass, so exam details (domains,
  question counts, concepts, prices, dates) are checked against official vendor
  docs (Microsoft Learn, AWS, ISC2, PMI, CompTIA). Every other post is rated
  without search first and only re-rated with search if it flags a
  `[UNVERIFIABLE]` claim. A shared per-run budget (`--max-searches`, default
  `MAX_WEB_SEARCHES` or 1500; 0 = unlimited) caps total grounded calls, so the
  search allowance is spent where it matters and Vertex grounding cannot run away
  with the project credits. When the budget is spent, remaining posts are rated
  without search and carry a `web-search budget exhausted` warning. The JSON
  summary reports `max_web_searches`, `search_calls` and `web_searches`.
  - On the Gemini Developer API the ~1500/day figure is a free-tier quota; on
    Vertex AI, grounding is **billed to the GCP project**, so the cap is a
    self-imposed budget.
- **Rubric strictness** (edited in `prompts/examos-blog-rating.md`): a *confirmed*
  factual error is a hard fail (score 1-2, never above 2); length/depth is judged by
  density with word count only a soft guide (padding penalised too); the voice/AI-slop
  criterion penalises every tell (empty intensifiers such as actually/genuinely, filler
  openers, "not just X, it's Y", scare quotes — quotation marks only for sources,
  defined terms and quoted exam questions); internal linking scores 5 when the body has
  no links (the expected post-stripper state) and 1 only if a link survives; and fewer
  than 4 FAQ questions caps that criterion at 2.
- Writes a combined markdown report (`reports/rating-*.md`, blocks separated by
  `---`) plus a JSON summary (`reports/rating-*.json`) with the rubric name,
  per-file grades, weighted scores and grade counts. Both are flushed after every
  completed file (same paths, overwritten), so Ctrl+C or a crash still leaves the
  ratings finished so far on disk. Content is never modified.

## Blog correction agent
Reads every content file under an explicit `--input`, repairs known issues, and
reports the changes. Deterministic fixes always run; the LLM only *proposes* values
(JSON) that Python validates before applying, so a bad response can never produce an
out-of-range title or broken frontmatter. Content is never modified in place.

1. **FAQ heading** — any variant (`## FAQ`, `## Frequently Asked Questions`,
   `### Fast Answers`) becomes the required `## Frequently Asked Questions (FAQ)`.
2. **Dangling endings** — `Other related exams:` lines left by the link stripper are
   removed and the surrounding blank runs collapsed.
3. **SEO metadata** (`--seo/--no-seo`, default **off**) — the corrector does **not**
   rewrite `title` / `seoTitle` / `seoDescription` by default; `fix-frontmatter` owns
   that. Pass `--seo` to re-enable the old pass (rewrite a field >10% over its maximum
   or missing the primary keyword; frontmatter edited per-key, never re-dumped).
4. **Missing or short FAQ** — when there is no FAQ section, or it has fewer than the
   required questions, the whole section is regenerated (never padded one question at a
   time, which invites duplicates). Questions are generated with Google Search grounding
   (counted against the shared `--max-searches` budget) and vendor sources are recorded.
5. **Primary keyword** (`--seo`, default off) — when enabled, the keyword is placed in
   the first 100 words via a minimal opening-paragraph rewrite; used only when the
   keyword is genuinely absent and the rewrite is valid.
6. **Factual corrections** (`--fix-facts/--no-fix-facts`, default on) — for exam-specific
   posts a grounded pass enumerates every exam code/cert/price/date and proposes
   `find -> replace` fixes with an official source. A fix is applied only when the exact
   text is present, the search string is at least 3 characters and looks like a fact (has
   a digit or an uppercase run, so `PMP`/`AWS` qualify but a plain word does not), and a
   source URL is given. Matching is word-boundary and every occurrence is replaced, so a
   code repeated in the body and FAQ is fixed everywhere. Best-effort, not a guarantee —
   review the diff and sources.
7. **Grammar** (`--fix-grammar/--no-fix-grammar`, default on) — a plain LLM pass proposes
   minimal fixes for sentence fragments, run-ons and dangling modifiers as
   `find -> replace`. A fix is applied only when the phrase is at least 12 characters;
   every occurrence is replaced (an exact repeated heading is fixed everywhere).
8. **AI slop** (`--fix-slop/--no-fix-slop`, default on) — a plain LLM pass rewrites the
   sentences that carry slop: empty intensifiers/hedges (very, really, truly, actually,
   genuinely, basically, simply, literally, including inside headings), filler openers
   ("A quick note before diving in", "Let's dive in"), the "It's not just X, it's Y"
   construction, em dashes/exclamation marks, and scare quotes around ordinary words.
   Same apply guard as grammar. With `--ratings <report.json>` the de-slop pass runs
   **only** on posts whose `voice_ai_slop` score is below `--slop-threshold` (default 3),
   so edits stay minimal; `--flagged-only` restricts the whole run to those files.
   `--min-grade A-` (or `--min-average 4.0`), read from the same report, instead restricts
   the run to posts whose **rating average is below that grade**, so a correction pass can
   target only the posts that scored badly overall.
   The report loader tolerates a malformed JSON report (falls back to block scanning).
   The rater scores this as **Voice and AI Slop** and now penalises
   every occurrence (a single tell means the criterion cannot score 5, and repeated tells
   or slop in a heading drop it to 1-2).
9. **ExamOS callouts** (`--fix-examos/--no-fix-examos`, default on) — when a post mentions
   ExamOS, a plain LLM pass flags invented features or a wrong booking trigger and rewrites
   the sentence using only the real features (Daily Sprint, Mock Exam, Readiness Score,
   Rookie/Challenger/Legend; booking triggered by the Readiness Score). Same apply guard.

Every phrase/fact rewrite is rejected when its length ratio falls outside a fixed band
(grammar up to 3x, de-slop up to 1.8x, facts up to 1.5x, and never below ~0.5x), so a
rambling or hallucinated rewrite cannot be applied.

The corrector prompt (`prompts/examos-blog-corrections.md`) and the rating rubric
(`prompts/examos-blog-rating.md`) are kept aligned on the shared rules: SEO lengths
(title 50-65, seoTitle 55-65/max 70, seoDescription 140-160), the required FAQ heading
`## Frequently Asked Questions (FAQ)`, the voice/AI-slop tell list, and the allowed
ExamOS features.

- Runs concurrently (`--workers`), with `--rate-limit`, `--max-attempts`, `--timeout`,
  `--ping` and the non-sticky DeepSeek `--fallback` exactly like the other LLM agents.
- `--apply` writes corrected files under `--output` (default `output_corrected/`); the
  input directory is never modified. Dry-run is the default.
- **Post-correction review** (`--review/--no-review`, default on): every corrected file is
  re-rated with `--rating-prompt` (default `prompts/examos-blog-rating.md`) and flagged
  in the report (`review_grade`, `review_average`, `review_flagged`) when its average is
  below `--review-threshold` (default 4.3, the A band). Review is **grounded** (a search
  model is built when `review` is on, even with `--no-verify-faq`) and reuses the shared
  search budget; it is skipped for files that were not changed. The run summary lists the
  flagged slugs for human review.
- Writes `reports/correct-*.diff` (unified diff) and `reports/correct-*.json`
  (per-file notes, warnings, sources, review grade/flags, error), flushed after every
  completed file. When review runs it also writes a normal rating report
  (**`reports/rating-*.md` / `.json`**, same format as `rate-blogs`) for the corrected
  posts, and — when any post is below the threshold — a focused
  **`reports/human-review-*.md`** listing each flagged post with the specific rubric
  feedback (weaknesses, factual issues, low-scoring criteria), what the corrector already
  changed, warnings and sources. Report writes are atomic (temp file + rename) and
  filenames are microsecond-unique, so concurrent runs can never corrupt each other.

## Frontmatter validator/corrector agent
Enforces a project's frontmatter schema on a whole corpus, changing the frontmatter
**only** — the body is reassembled byte-for-byte verbatim. The schema is external
(`prompts/*.md`) so pointing `--prompt` at a new file retargets the agent at another
project's frontmatter with no code change.

**The prompt carries the schema.** Two formats are supported:

- **Markdown tables** (the ExamOS `examos-blog-frontmatter.md` layout): a
  `FRONTMATTER SCHEMA` table (`Order | Field | Required | Rules`), an `Authors` table,
  a `Categories` list, an `Exam Codes` list and a field-name alias table. The parser
  reads the field order, required flags, fixed constants (from "must be exactly `x`"),
  length targets and aliases straight out of the tables, so the prompt stays the single
  source of truth.
- **YAML frontmatter** (optional, takes precedence): `fields`, `authors`,
  `categories`, `exam_codes`, `aliases`, `category_synonyms`, `exam_aliases`,
  `document_type`. Its body is the system prompt used only by the optional LLM pass.

**Deterministic by default** (no key, no LLM):

1. **Order** — fields are re-emitted in the schema sequence; unknown fields are
   preserved at the end and flagged.
2. **Names** — aliases are renamed (`examCode` → `examCodes`, `seo_title` → `seoTitle`,
   `pubDate`/`publishedAt` → `date`, `authour` → `author`, …) and duplicate fields keep
   the first occurrence.
3. **Fixed values** — `type` and `imagePosition` are forced to the schema constant.
4. **Approved values** — an unapproved `author` is replaced by the closest name match
   (then by the specialty of the post's primary category); unapproved categories are
   mapped through a curated synonym table (`pmp` → `project-management`, `ml` → `mlops`,
   …), then by fuzzy match, else dropped and flagged; `categories` are sorted
   alphabetically; invalid `examCodes` are removed.
5. **`examCodes` detection** — every valid exam the content references is detected and
   filled so related posts can be tied together: explicit code mentions (tolerant of
   `-`/`_`/space) plus exam names from the exam database (`--exam-db`, default
   `exam_database.txt`) and curated aliases (`Google Professional Cloud Architect` →
   `GCP-PCA`, `SAFe Agilist` → `SA_6`). Broad name aliases run in metadata only and
   conservative aliases in the body, so `Security + Data` is never read as `Security+`.
   Existing valid codes are preserved and merged; first/score metadata matches come
   first, then body mentions.
6. **Missing required fields** — best-effort defaults: `slug` from the filename,
   `title` from the H1, `seoTitle` from the title, `seoDescription` from the first
   paragraph, `author`/`categories` inferred from the exam code or topic, `date` today,
   `order` 900. Every inference is flagged for human review.
7. **SEO metadata (LLM)** — `title` (50-65), `seoTitle` (55-65) and `seoDescription`
   (140-160) that are missing or outside their target range are rewritten from the slug
   and body using `--seo-prompt` (`prompts/examos_blog_seo_rewritre.md`; `--seo`, default
   on, `--no-seo` for rules-only). Only validated candidates are applied (length in range,
   no `!`/em dash, no filler, `seoTitle` different from `title`), with one corrective
   retry; anything still invalid is flagged, **never trimmed**.
8. **Study-plan fields** on a blog (`provider`, `estimatedHours`, `passingScore`,
   `targetAudience`) are removed; a study-plan document keeps them and uses `examCode`.
9. **Consistency** — string values are quoted and spacing normalised, so the same field
   always looks the same.

**Order file.** `--order-file prompts/examos-blog-order.json` maps `slug` → integer and
sets each post's `order` (merged into a full pass, or the only change with `--only-order`,
which does a targeted line replacement and touches nothing else). Slugs in the file with
no matching content file are listed in the report.

**LLM passes** use Gemini on Vertex AI (same backend/retry/fallback machinery as the
other agents). The SEO rewriter (`--seo`, default on) rewrites out-of-range metadata; the
`--llm` pass (default off) also fills other missing required fields; in both cases Python
validates every proposal before applying it.

### Output
- Dry-run writes `reports/frontmatter-*.diff` (frontmatter-only unified diff),
  `reports/frontmatter-*.json` and a `reports/frontmatter-*.md` **changelog** (per-file
  before/after table + items requiring human review), flushed after every file.
- `--apply` writes corrected files under `--output` (default `output_frontmatter/`),
  preserving relative paths; the input directory is never modified.

## Fact checker agent
Validates content against the manually verified fact files in `facts/` (one JSON per exam,
named after the exam slug) and corrects the discrepancies that can be fixed safely. The
**fact files are the source of truth and supersede any earlier agent's edits.**

- **Linking.** A file is linked to exams by its frontmatter `examCodes` (primary) or, when
  absent, by detecting exam codes/names from the fact registry in metadata (primary) or the
  body (secondary). Content that references no exam is skipped; not all content has an exam
  and not all exams have content.
- **Deterministic checks** (no key, no LLM): service/product `renames`; retired/retiring and
  out-of-scope service mentions; price; passing score; duration; question counts (exact and
  range); predecessor retirement date, GA date and last day to take; exam status; domain
  `weight_percent` mismatches and grouped-domain math that cannot sum to the fact sheet;
  cross-cloud vendor contamination (a competitor service, e.g. AWS CloudWatch in an Azure
  post), ignoring deliberate comparisons and multi-cloud posts.
- **Corrections are conservative.** A value is auto-applied only when the exam is *active*,
  the fact is `exact`, the document links exactly **one primary** exam, and the claim is
  clearly tied to it. A rename is skipped when it appears in a rename discussion, when
  linked exams give conflicting successors, or when it is not tied to a primary exam. Ranges
  ("40-60 questions"), salary figures, and unrelated numbers are ignored. Everything else is
  reported and listed for human review, never guessed.
- **Optional LLM pass** (`--llm`, default off): the content-type prompt
  (`prompts/examos-content-facts.md`, `--prompt`) plus the fact sheet are sent to Gemini.
  The prompt covers the same areas plus domain-weight math and cross-cloud vendor
  contamination, and pins an exact JSON schema (`issues` of `find`/`replace`/`reason`/
  `source`, plus `unresolved`). Python validates each fix (exact match, source URL, length
  band) before applying, so a bad response cannot corrupt content. Swapping the prompt
  retargets the pass at study plans, roadmaps or FAQs.

### Output
- Always writes `reports/factcheck-*.md` (human-review report: every applied fix and every
  unresolved flag), `reports/factcheck-*.json` (per-file exams, findings, counts, sources)
  and `reports/factcheck-*.diff` (unified diff), flushed after every file.
- `--apply` writes corrected files under `--output` (default `output_factchecked/`); the
  input directory is never modified. `--no-fix` reports without changing anything.

## Agent roadmap (future)
- **Writer:** draft/regenerate posts.

## Environment
Copy `.env.example` → `.env`. See `GEMINI_*` keys (primary) and `DEEPSEEK_*`
keys (fallback). Rules-only mode needs no key.

**Vertex AI backend** (`GEMINI_BACKEND=vertex`, default when a GCP project is set):
set `GOOGLE_CLOUD_PROJECT` and `GOOGLE_CLOUD_REGION`, then authenticate with
Application Default Credentials (`gcloud auth application-default login`, then
`gcloud auth application-default set-quota-project <project>`) or point
`GOOGLE_APPLICATION_CREDENTIALS` at a service-account JSON. Requests go to
`aiplatform.googleapis.com` and bill your GCP project. `GOOGLE_CLOUD_REGION` must be a
region (`asia-south1`); a gcloud *zone* like `asia-south1-a` is auto-reduced to the
region with a warning (a zone builds an invalid endpoint and yields a confusing 404).
The backend is only considered available when a credential file is discoverable, so a
missing ADC fails fast instead of mid-batch.

**Gemini Developer API backend** (`GEMINI_BACKEND=developer`): set `GEMINI_API_KEY`.

## Commands
```
pip install -e '.[dev]'
contentos strip-links --input blog --reports reports            # dry-run (diff only)
contentos strip-links --input blog --output output --reports reports --apply
contentos interlink --input blog --reports reports              # dry-run (analysis only)
contentos interlink --input blog --reports reports --apply      # merge into output/interlinks.json
contentos interlink --slug how-hard-is-ai-103 --limit 1         # single source
contentos interlink --workers 4 --rate-limit 2 --max-attempts 5 # throttle + retries
contentos review-interlinks --mapping output/interlinks.json    # dry-run review
contentos review-interlinks --mapping output/interlinks.json --apply
contentos review-interlinks --no-redo --apply                   # deterministic fixes only
contentos review-interlinks --no-redo --no-ping                 # rules-only, no LLM/key
contentos rate-blogs --input blog --reports reports             # grade every post A+ to F
contentos rate-blogs --input output --workers 4 --rate-limit 2  # throttle + retries
contentos rate-blogs --input blog --prompt prompts/my-rubric.md # custom rubric
contentos rate-blogs --input blog --max-searches 200            # cap search-grounded calls
contentos correct-blogs --input output --reports reports        # dry-run correction diff
contentos correct-blogs --input output --apply                  # write to output_corrected/
contentos correct-blogs --slug ai-103-vs-ai-200-which-should-you-take
contentos correct-blogs --input output --ratings reports/rating-*.json   # de-slop only flagged posts
contentos correct-blogs --input output --ratings reports/rating-*.json --flagged-only  # only AI-slop-flagged files
contentos correct-blogs --input output --ratings reports/rating-*.json --min-grade A-  # only posts below A-
contentos fix-frontmatter --input blog --reports reports                     # dry-run schema check
contentos fix-frontmatter --input blog --reports reports --apply             # write to output_frontmatter/
contentos fix-frontmatter --input blog --order-file prompts/examos-blog-order.json --only-order --apply
contentos fix-frontmatter --input blog --prompt prompts/other-project-frontmatter.md
contentos check-facts --input blog --reports reports                  # dry-run, deterministic
contentos check-facts --input output_frontmatter --reports reports --apply
contentos check-facts --input blog --no-fix                           # flag only
contentos check-facts --input blog --llm                              # add the optional LLM pass
```
