# contentos

Agent-based content pipeline for SEO. It produces content that ranks and turns
impressions into clicks. Agents cover **strip / interlink / review / rate / correct /
frontmatter / fact-check** (a writer is planned) and run as standalone CLI commands over
a shared skeleton.

- **Language:** Python 3.11+
- **LLM:** Google Gemini with optional Google Search grounding. Two backends:
  **Vertex AI** (`aiplatform.googleapis.com`, via `google-cloud-aiplatform`, bills
  your GCP project) or the **Gemini Developer API** (API key). DeepSeek API as the
  fallback provider.
- **CLI:** Typer. **Reports:** Markdown, JSON, unified diff.

---

## Install

```bash
pip install -e '.[dev]'
```

## Configure

Copy `.env.example` to `.env` and fill in your values.

### Backend selection

| Variable | Required | Purpose |
|---|---|---|
| `GEMINI_BACKEND` | no | `vertex` or `developer`. Auto-detected: Vertex if a GCP project is set, else Developer. |
| `GEMINI_MODEL` | no | Overrides the backend default (vertex `gemini-2.5-flash`, developer `gemini-3.6-flash`). |

### Vertex AI backend (`GEMINI_BACKEND=vertex`)

| Variable | Required | Purpose |
|---|---|---|
| `GOOGLE_CLOUD_PROJECT` | yes | GCP project id (billed). `VERTEX_PROJECT` also works. |
| `GOOGLE_CLOUD_REGION` | no | Default `us-central1`. `VERTEX_LOCATION` also works. Use a region (`asia-south1`); a zone like `asia-south1-a` is auto-reduced with a warning. |

Auth uses **Application Default Credentials**. Set it up one of two ways:

```bash
# Option A: user credentials via gcloud
gcloud auth application-default login
gcloud auth application-default set-quota-project <your-project-id>

# Option B: service account (set the env var to the JSON key path)
GOOGLE_APPLICATION_CREDENTIALS=C:\path\to\service-account.json
```

The backend is only considered available when a credential file is discoverable, so
a missing ADC fails fast at startup instead of mid-batch.

### Web search budget

| Variable | Required | Purpose |
|---|---|---|
| `MAX_WEB_SEARCHES` | no | Default cap on Google Search grounded calls per run, used by `rate-blogs` (default `1500`, `0` = unlimited). |

On the Gemini Developer API free tier this mirrors the ~1500/day free search
quota; on Vertex AI grounding is billed to the GCP project, so it is a
self-imposed budget. Override per run with `--max-searches N`.

### Gemini Developer API backend (`GEMINI_BACKEND=developer`)

| Variable | Required | Purpose |
|---|---|---|
| `GEMINI_API_KEY` | yes | Google AI Studio key. `GOOGLE_API_KEY` also works. |

### Fallback

| Variable | Required | Purpose |
|---|---|---|
| `DEEPSEEK_API_KEY` | no | Enables the automatic fallback provider. |
| `DEEPSEEK_MODEL` | no | Default `deepseek-chat`. |
| `DEEPSEEK_BASE_URL` | no | Default `https://api.deepseek.com/v1`. |

**Provider chain:** Gemini is always primary. When a Gemini call times out or the
connection drops, that single call is retried against DeepSeek (`--fallback`, on by
default); the fallback is **not sticky**, so the next call starts with Gemini again.
Auth/credential errors are fatal and abort the run rather than retrying. Rules-only
commands need no key.

**Web search:** Gemini's Google Search grounding is used by the rating agent for
fact verification (`--verify-facts`, on by default):

- **Exam-specific posts** (study guides, "is it hard", "is it worth it",
  comparisons, and any post whose slug/title names an exam or certification) are
  rated with search on the first pass, so exam details (domains, question counts,
  concepts, prices, dates) are checked against official vendor docs such as
  Microsoft Learn, AWS, ISC2, PMI and CompTIA.
- **All other posts** are rated without search first; they only get a search pass
  if they flag a `[UNVERIFIABLE]` claim.

Disable entirely with `--no-verify-facts`. A shared per-run budget
(`--max-searches`, default `MAX_WEB_SEARCHES` or 1500) caps total grounded calls;
posts rated after it is spent carry a `web-search budget exhausted` warning. The
JSON summary reports `max_web_searches`, `search_calls` and `web_searches`.

Run `contentos --help` for the command list.

---

## Pipeline overview

| Agent | Command | LLM? | Writes |
|---|---|---|---|
| 1. Link stripper | `strip-links` | no | diff report; cleaned files with `--apply` |
| 2. Interlinker | `interlink` | yes | analysis report; mapping with `--apply` |
| 3. Interlink reviewer | `review-interlinks` | optional (redo) | review report; repaired mapping with `--apply` |
| 4. Blog rater | `rate-blogs` | yes | combined markdown + JSON summary |
| 5. Blog corrector | `correct-blogs` | yes | diff report; corrected files with `--apply` |
| 6. Frontmatter fixer | `fix-frontmatter` | yes (`--seo` on) | diff + JSON + changelog; corrected files with `--apply` |
| 7. Fact checker | `check-facts` | optional (`--llm`) | diff + JSON + human-review; corrected files with `--apply` |

`blog/` is never mutated in place. `--apply` writes to a separate `--output`.

---

## 1. `strip-links` — remove hyperlinks

Deterministic, no LLM. Removes embed components, label+link blocks, standalone
link lines, inline links (keeps the anchor text), bare URLs, then cleans up.

```
contentos strip-links [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `--input`, `-i` | `blog` | Folder of content files. |
| `--output` | — | Where cleaned files go (**required** with `--apply`). |
| `--reports` | `reports` | Where reports go. |
| `--apply` | off | Write cleaned files to `--output`. |

```bash
contentos strip-links --input blog --reports reports              # dry-run (diff only)
contentos strip-links --input blog --output output --reports reports --apply
```

Outputs: `reports/strip_links-*.diff`, `reports/strip_links-*.json`.

---

## 2. `interlink` — generate internal links

The LLM reads each source + the full inventory, scores every candidate and
selects links; Python enforces the hard rules (positions, scores, quizzes,
anchors, no self-links). The LLM never writes content.

```
contentos interlink [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `--input`, `-i` | `blog` | Folder of source content. |
| `--inventory` | `input/content_inventory.json` | Content inventory. |
| `--output` | `output/interlinks.json` | Mapping file (a directory gets `interlinks.json` inside it). |
| `--reports` | `reports` | Where reports go. |
| `--apply` | off | Merge the mapping into `--output`. |
| `--limit` | — | Process only the first N sources. |
| `--slug` | — | Process a single source slug. |
| `--workers`, `-w` | `4` | Concurrent LLM threads. |
| `--rate-limit` | `0.0` | Max LLM requests/sec (0 = unlimited). |
| `--max-attempts` | `5` | Retries per call on 429/5xx/timeouts. |
| `--timeout` | `120.0` | Per-request timeout (seconds). |
| `--fallback` / `--no-fallback` | on | Switch to DeepSeek if Gemini times out. |
| `--ping` / `--no-ping` | on | Connectivity check before the run. |
| `--quiet`, `-q` | off | Hide live progress; print a detailed summary. |

```bash
contentos interlink --input blog --reports reports              # dry-run (analysis only)
contentos interlink --input blog --reports reports --apply      # merge into output/interlinks.json
contentos interlink --slug how-hard-is-ai-103 --limit 1         # single source
contentos interlink --workers 4 --rate-limit 2 --max-attempts 5 # throttle + retries
```

Outputs: `reports/interlink-*.json`; `--apply` merges into `--output`.

---

## 3. `review-interlinks` — repair an existing mapping

Deterministic repairs plus an optional targeted LLM redo:

1. **Reciprocal inline links** — the weaker direction of an inline A↔B pair is
   dropped (ranked by exam-code relevance, position prominence, target type).
2. **`100%` links** — promoted to `related` (end-of-article duplicates the
   Related Reading section).
3. **Misplaced quizzes** — moved to a free `50%`/`75%` slot or dropped.
4. **Thin Related sections** — sources with exactly one `related` item are
   re-analysed with the LLM and only their entry is replaced.

```
contentos review-interlinks [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `--mapping` | `output/interlinks.json` | Mapping to review. |
| `--input`, `-i` | `blog` | Source content folder (for redo). |
| `--inventory` | `input/content_inventory.json` | Content inventory. |
| `--output` | `--mapping` | Where the fixed mapping goes. |
| `--reports` | `reports` | Where reports go. |
| `--apply` | off | Write the repaired mapping. |
| `--redo` / `--no-redo` | on | Re-run the LLM for single-related sources. |
| `--single-related` | `1` | Redo sources whose related count equals this. |
| `--workers`, `-w` | `4` | Concurrent LLM threads. |
| `--rate-limit` | `0.0` | Max LLM requests/sec (0 = unlimited). |
| `--max-attempts` | `5` | Retries per call. |
| `--timeout` | `120.0` | Per-request timeout (seconds). |
| `--fallback` / `--no-fallback` | on | Switch to DeepSeek if Gemini times out. |
| `--ping` / `--no-ping` | on | Connectivity check. |
| `--limit` | — | Redo only the first N flagged sources. |
| `--slug` | — | Redo a single flagged source. |
| `--quiet`, `-q` | off | Hide live progress. |

```bash
contentos review-interlinks --mapping output/interlinks.json              # dry-run
contentos review-interlinks --mapping output/interlinks.json --apply
contentos review-interlinks --no-redo --apply                             # deterministic fixes only
contentos review-interlinks --no-redo --no-ping                           # rules-only, no LLM/key
```

Outputs: `reports/review-*.json`; `--apply` writes the repaired mapping.

---

## 4. `rate-blogs` — grade content A+ to F

Read-only audit. The LLM scores every rubric criterion 1-5 and returns JSON per
post; Python clamps the scores, computes the weighted total and letter grade, and
renders the report, so the table, total and grade always agree.

```
contentos rate-blogs [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `--input`, `-i` | **required** | Folder of content files to rate. |
| `--reports` | `reports` | Where reports go. |
| `--prompt` | `prompts/examos-blog-rating.md` | Rubric prompt file. |
| `--limit` | — | Rate only the first N files. |
| `--slug` | — | Rate a single file slug. |
| `--workers`, `-w` | `4` | Concurrent LLM threads. |
| `--rate-limit` | `0.0` | Max LLM requests/sec (0 = unlimited). |
| `--max-attempts` | `5` | Retries per call. |
| `--timeout` | `120.0` | Per-request timeout (seconds). |
| `--fallback` / `--no-fallback` | on | Switch to DeepSeek if Gemini times out. |
| `--verify-facts` / `--no-verify-facts` | on | Re-check `[UNVERIFIABLE]` claims with Google Search. |
| `--max-searches` | `MAX_WEB_SEARCHES` or `1500` | Cap on search-grounded calls for the run (0 = unlimited). |
| `--ping` / `--no-ping` | on | Connectivity check. |
| `--quiet`, `-q` | off | Hide live progress. |

```bash
contentos rate-blogs --input blog --reports reports              # grade every post
contentos rate-blogs --input output --workers 4 --rate-limit 2   # throttle + retries
contentos rate-blogs --input blog --prompt prompts/my-rubric.md  # custom rubric
contentos rate-blogs --input blog --no-verify-facts              # skip web search
contentos rate-blogs --input blog --max-searches 200             # cap search calls
```

Outputs: `reports/rating-*.md` (one block per post, separated by `---`) and
`reports/rating-*.json` (rubric name, per-file grades/scores, grade counts,
`max_web_searches`, `search_calls`, `web_searches`). Both are rewritten after each
completed post, so interrupting the run still leaves every rating finished so far
on disk.

### Custom rubrics

Rubrics live in `prompts/*.md` as Markdown with YAML frontmatter:

```markdown
---
name: My Rubric
criteria:
  - {key: clarity, label: Clarity, weight: 1}
  - {key: accuracy, label: Accuracy, weight: 3}
grade_bands:
  - {min: 4.5, label: PASS}
  - {min: 0.0, label: FAIL}
---
<system prompt body: describe each criterion and how to score it>
```

The code auto-appends the JSON output contract from the `criteria` keys, so a new
rubric only needs its criterion descriptions. Point `--prompt` at it.

---

## 5. `correct-blogs` — repair content

Deterministic repairs always run; the LLM only proposes values (JSON) that Python
validates before applying, so a bad response can never produce an out-of-range title
or broken frontmatter. Content is never modified in place.

- **FAQ heading** — any variant (`## FAQ`, `## Frequently Asked Questions`,
  `### Fast Answers`) becomes `## Frequently Asked Questions (FAQ)`.
- **Dangling endings** — `Other related exams:` lines are removed.
- **SEO metadata** (`--seo`, default **off**) — the corrector does not rewrite
  `title` / `seoTitle` / `seoDescription` by default; `fix-frontmatter` owns SEO. Pass
  `--seo` to re-enable the old pass (rewrite a field more than 10% over its maximum or
  missing the primary keyword, and place the keyword in the first 100 words).
- **Missing or short FAQ** — when there is no FAQ section, or fewer than four questions,
  the section is regenerated with Google Search grounding (counted against
  `--max-searches`) and the sources are recorded in the report.
- **Factual corrections** (`--fix-facts`, default on) — for exam posts a grounded pass
  proposes sourced `find -> replace` fixes for wrong exam codes, prices and dates. A fix
  is applied only when the exact text is present (word-boundary, so short codes like
  `PMP`/`AWS` are safe) and an official source is given. Best-effort — review the diff.
- **Grammar** (`--fix-grammar`, default on) — a plain LLM pass proposes minimal fixes for
  sentence fragments, run-ons and dangling modifiers; a phrase is only replaced when it
  is at least 12 characters (every occurrence is replaced).
- **AI slop** (`--fix-slop`, default on) — rewrites sentences with empty intensifiers
  (actually, genuinely, ...), filler openers, "It's not just X, it's Y", em dashes and
  scare quotes around ordinary words. Pass `--ratings <report.json>` to run it **only**
  on posts scoring below `--slop-threshold` (default 3) on Voice/AI Slop. To correct only
  the posts that scored badly overall, pass `--ratings` with `--min-grade A-` (or
  `--min-average 4.0`): the run is restricted to posts whose rating average is below that
  band, and all correction passes then run on just those files.
- **ExamOS callouts** (`--fix-examos`, default on) — when a post mentions ExamOS, fixes
  invented features or a wrong booking trigger using only the real features (Daily
  Sprint, Mock Exam, Readiness Score, Rookie/Challenger/Legend).
- **Post-correction review** (`--review`, default on) — re-rates every corrected post and
  flags any whose average is below `--review-threshold` (default 4.3, the A band) in the
  report and run summary. Review is grounded for exam posts and reuses `--max-searches`.

Every rewrite must also stay within a length band relative to the original it replaces
(grammar up to 3x, de-slop 1.8x, facts 1.5x, never below ~0.5x), so a rambling or
hallucinated rewrite is rejected and recorded as a warning.

```
contentos correct-blogs [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `--input`, `-i` | **required** | Folder of content files to correct. |
| `--output` | `output_corrected` | Where corrected files go in apply mode. |
| `--reports` | `reports` | Where reports go. |
| `--prompt` | `prompts/examos-blog-corrections.md` | Correction prompt file. |
| `--apply` | off | Write corrected files to `--output` (input is never modified). |
| `--limit` | — | Correct only the first N files. |
| `--slug` | — | Correct a single file slug. |
| `--workers`, `-w` | `4` | Concurrent LLM threads. |
| `--rate-limit` | `0.0` | Max LLM requests/sec (0 = unlimited). |
| `--max-attempts` | `5` | Retries per call. |
| `--timeout` | `120.0` | Per-request timeout (seconds). |
| `--fallback` / `--no-fallback` | on | Switch to DeepSeek if Gemini times out (per call). |
| `--verify-faq` / `--no-verify-faq` | on | Generate/regenerate FAQ sections with Google Search. |
| `--fix-facts` / `--no-fix-facts` | on | Search-verify exam posts and apply sourced factual fixes. |
| `--fix-grammar` / `--no-fix-grammar` | on | Apply minimal fixes for fragments and grammar errors. |
| `--fix-slop` / `--no-fix-slop` | on | Remove AI-slop tells (intensifiers, filler, scare quotes). |
| `--fix-examos` / `--no-fix-examos` | on | Fix invented ExamOS features / wrong booking triggers. |
| `--seo` / `--no-seo` | **off** | Also rewrite `title`/`seoTitle`/`seoDescription`. Off: `fix-frontmatter` owns SEO. |
| `--review` / `--no-review` | on | Re-rate each corrected post and flag any below `--review-threshold`. |
| `--review-threshold` | `4.3` | Average a corrected post must reach (4.3 = the A band). |
| `--rating-prompt` | `prompts/examos-blog-rating.md` | Rubric used for the post-correction review. |
| `--ratings` | — | Rating report JSON; gates de-slop and the grade filter. |
| `--slop-threshold` | `3.0` | Voice/AI-Slop score below which a post is de-slopped (with `--ratings`). |
| `--min-grade` | — | Only correct posts graded below this letter (e.g. `A-`); requires `--ratings`. |
| `--min-average` | — | Only correct posts whose rating average is below this (e.g. `4.0` = A-). |
| `--flagged-only` | off | With `--ratings`, restrict the run to the AI-slop-flagged posts. |
| `--max-searches` | `MAX_WEB_SEARCHES` or `1500` | Cap on search-grounded calls for the run. |
| `--ping` / `--no-ping` | on | Connectivity check. |
| `--quiet`, `-q` | off | Hide live progress. |

```bash
contentos correct-blogs --input output --reports reports        # dry-run diff
contentos correct-blogs --input output --apply                  # write to output_corrected/
contentos correct-blogs --input output --no-verify-faq          # skip FAQ generation
contentos correct-blogs --input output --ratings reports/rating-*.json --flagged-only  # only AI-slop-flagged posts
contentos correct-blogs --input output --ratings reports/rating-*.json --min-grade A-  # only posts below A-
contentos correct-blogs --slug ai-103-vs-ai-200-which-should-you-take
```

Outputs: `reports/correct-*.diff` (unified diff) and `reports/correct-*.json`
(per-file notes, warnings, grounding sources, errors), flushed after every file. With
review on, the corrected posts are also written as a normal rating report
(`reports/rating-*.md` / `.json`) and, if any fall below the threshold, a focused
`reports/human-review-*.md` lists each flagged post with its specific issues. Writes are
atomic with microsecond-unique filenames.

---

## 6. `fix-frontmatter` — enforce the frontmatter schema

Validates and corrects frontmatter across a corpus, changing the frontmatter **only**;
the body is preserved byte for byte. Structure is fixed deterministically; `title`,
`seoTitle` and `seoDescription` are **rewritten with the LLM** when they are missing or
outside their target range (`--seo`, default on; `--no-seo` runs rules-only). The schema
lives in an external prompt file, so pointing `--prompt` at another project's prompt
retargets the agent with no code change.

The prompt (`prompts/examos-blog-frontmatter.md`) carries the schema as Markdown tables
(field order, required flags, fixed values, approved authors, categories, exam codes and
field-name aliases). An optional YAML frontmatter in the prompt can declare the same
explicitly (`fields`, `authors`, `categories`, `exam_codes`, `aliases`,
`category_synonyms`, `exam_aliases`, `document_type`) and takes precedence. The SEO
rewriter prompt is separate (`prompts/examos_blog_seo_rewritre.md`, `--seo-prompt`).

What it fixes:

- **Field order** — re-emitted in the schema sequence; unknown fields are preserved at
  the end and flagged.
- **Field names** — aliases renamed (`examCode` → `examCodes`, `seo_title` → `seoTitle`,
  `pubDate`/`publishedAt` → `date`, `authour` → `author`); duplicates keep the first.
- **Fixed values** — `type` and `imagePosition` forced to the schema constant.
- **Approved values** — unapproved authors replaced by the closest name or the
  specialty of the post's category; categories mapped through a synonym table
  (`pmp` → `project-management`, `ml` → `mlops`), then fuzzy match, else dropped and
  flagged; categories sorted; invalid `examCodes` removed.
- **`examCodes`** — every valid exam the content references is detected and filled, so
  related posts can be tied together. Explicit codes are matched, plus exam names from
  the exam database (`--exam-db`, default `exam_database.txt`) and a small curated alias
  table (`Google Professional Cloud Architect` → `GCP-PCA`, `SAFe Agilist` → `SA_6`).
  Detection is metadata-first (`score` order), preserves existing valid codes and drops
  invalid ones.
- **SEO metadata (LLM)** — `title` (50-65), `seoTitle` (55-65) and `seoDescription`
  (140-160) that are missing, too short, too long, or carry a banned tell (`!`, em dash,
  filler) are rewritten from the slug and body with `--seo-prompt`. Only validated
  candidates are applied (in-range length, no exclamation/em dash, no filler, `seoTitle`
  different from `title`), with one corrective retry; anything still invalid is flagged,
  never trimmed. `--no-seo` leaves out-of-range fields flagged for a human.
- **Missing required fields** — inferred (slug from filename, title from H1, author and
  categories from the exam code/topic, date today, order 900) and flagged for review.
- **Study-plan fields on blogs** — `provider`, `estimatedHours`, `passingScore` and
  `targetAudience` removed (kept on study plans, which use singular `examCode`).
- **Consistency** — strings quoted and spacing normalised, so the same field always
  looks the same.

```
contentos fix-frontmatter [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `--input`, `-i` | **required** | Folder of content files to validate. |
| `--output` | `output_frontmatter` | Where corrected files go in apply mode. |
| `--reports` | `reports` | Where reports go. |
| `--prompt` | `prompts/examos-blog-frontmatter.md` | Frontmatter schema prompt. |
| `--apply` | off | Write corrected files to `--output` (input is never modified). |
| `--limit` | — | Process only the first N files. |
| `--slug` | — | Process a single file slug. |
| `--order-file` | — | JSON file mapping slug → integer; sets each post's `order`. |
| `--only-order` | off | Only update `order` from `--order-file`; touch nothing else. |
| `--exam-db` | `exam_database.txt` | Exam registry (`{code, name}` JSON) used to detect/fill `examCodes`. Empty string disables name detection. |
| `--seo` / `--no-seo` | on | Rewrite out-of-range `title`/`seoTitle`/`seoDescription` with the LLM. |
| `--seo-prompt` | `prompts/examos_blog_seo_rewritre.md` | SEO rewrite prompt file. |
| `--llm` / `--no-llm` | off | Let Gemini also infer other values the rules cannot. |
| `--workers`, `-w` | `4` | Concurrent worker threads. |
| `--rate-limit` | `0.0` | Max LLM requests/sec (0 = unlimited). |
| `--max-attempts` | `5` | Retries per LLM call. |
| `--timeout` | `120.0` | Per-request LLM timeout (seconds). |
| `--fallback` / `--no-fallback` | on | Switch to DeepSeek if Gemini times out. |
| `--ping` / `--no-ping` | on | Connectivity check before an `--seo`/`--llm` run. |
| `--quiet`, `-q` | off | Hide live progress. |

```bash
contentos fix-frontmatter --input blog --reports reports                 # dry-run (structure + SEO rewrite)
contentos fix-frontmatter --input blog --reports reports --apply         # write to output_frontmatter/
contentos fix-frontmatter --input blog --reports reports --no-seo        # rules-only, no key/LLM
contentos fix-frontmatter --input blog --order-file prompts/examos-blog-order.json --only-order --apply
contentos fix-frontmatter --input blog --prompt prompts/other-project-frontmatter.md
```

Outputs: `reports/frontmatter-*.diff` (frontmatter-only unified diff),
`reports/frontmatter-*.json` (per-file notes, warnings, review items, field changes) and
`reports/frontmatter-*.md` (a changelog with a before/after table and the items requiring
human review), flushed after every file. `--apply` writes changed files under `--output`,
preserving relative paths; the input directory is never modified.

---

## 7. `check-facts` — validate content against verified facts

Compares each content file against the manually verified fact files in `facts/` (one
JSON per exam, named after the exam slug) and corrects the discrepancies that can be
fixed safely. Content is linked to exams by its frontmatter `examCodes` (or, when absent,
by detecting exam codes/names from the fact registry); content that references no exam is
skipped. Written corrections replace earlier ones — **the facts supersede**.

Deterministic by default (no key, no LLM):

- **Service/product names** — a fact file's `renames` are applied (`Azure Active
  Directory` → `Microsoft Entra ID`, `Azure AI Foundry` → `Microsoft Foundry`, …). A
  rename is skipped (and flagged) when the old name appears in a rename discussion, when
  different exams give conflicting successors, or when it is not tied to the exam.
- **Retired / out-of-scope services** — a mention of a retired or retiring service, or a
  service the fact lists as out of scope, is flagged with its status, successor and source.
- **Price** — a wrong exam fee is flagged, and corrected when it is adjacent to the exam.
- **Passing score / duration / question count** — wrong values are corrected for a single
  clearly-linked active exam and flagged otherwise (ranges such as "40-60 questions" and
  salary numbers are ignored).
- **Dates** — a wrong predecessor retirement date, GA date or last day to take is
  corrected when the subject precedes the date, and flagged otherwise.
- **Domain names and weights** — a domain stated with the wrong `weight_percent` is flagged,
  and a grouped claim ("the two GenAI domains represent 30-40%") is flagged when no group of
  that many domains can total the claimed range on the fact sheet.
- **Vendor contamination** — a competitor cloud service (AWS CloudWatch, Amazon S3, Google
  BigQuery, …) used where the exam vendor's native service belongs is flagged; deliberate
  cross-vendor comparisons and multi-cloud posts are ignored.
- **Status** — an exam the facts mark retired, mentioned as if current, is flagged.

**Corrections are conservative.** A value is auto-applied only when the exam is *active*,
the fact is *exact*, the document links exactly **one primary** exam, and the claim is
clearly tied to it. Everything else — plus an optional LLM pass — is reported and listed
for human review.

The prompt file (`prompts/examos-content-facts.md`, `--prompt`) carries the content-type
guidance for the optional LLM pass, so the same program can fact-check blogs, study plans,
roadmaps or FAQs with a different prompt. It checks the same areas as the deterministic
engine plus domain-weight math and cross-cloud vendor contamination, and defines an exact
JSON output schema (`issues` with `find`/`replace`/`reason`/`source`, and `unresolved`) so
the response is always machine-parseable.

```
contentos check-facts [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `--input`, `-i` | **required** | Folder of content files to fact-check. |
| `--facts` | `facts` | Folder of verified fact JSON files. |
| `--output` | `output_factchecked` | Where corrected files go in apply mode. |
| `--reports` | `reports` | Where reports go. |
| `--prompt` | `prompts/examos-content-facts.md` | Content-type prompt for the optional LLM pass. |
| `--apply` | off | Write corrected files to `--output` (input is never modified). |
| `--fix` / `--no-fix` | on | Apply the safe deterministic corrections; otherwise flag everything. |
| `--limit` | — | Check only the first N files. |
| `--slug` | — | Check a single file slug. |
| `--llm` / `--no-llm` | off | Also run the LLM pass (prompt + fact sheet). |
| `--workers`, `-w` | `4` | Concurrent worker threads. |
| `--rate-limit` | `0.0` | Max LLM requests/sec (0 = unlimited). |
| `--max-attempts` | `5` | Retries per LLM call. |
| `--timeout` | `120.0` | Per-request LLM timeout (seconds). |
| `--fallback` / `--no-fallback` | on | Switch to DeepSeek if Gemini times out. |
| `--ping` / `--no-ping` | on | Connectivity check before an `--llm` run. |
| `--quiet`, `-q` | off | Hide live progress. |

```bash
contentos check-facts --input blog --reports reports                 # dry-run, no LLM
contentos check-facts --input output_frontmatter --reports reports --apply
contentos check-facts --input blog --no-fix                          # flag only, change nothing
contentos check-facts --input blog --llm                             # add the LLM pass
```

Outputs: `reports/factcheck-*.diff` (unified diff), `reports/factcheck-*.json` (per-file
exams, findings, applied/unresolved counts, sources) and `reports/factcheck-*.md` (the
human-review report listing every applied fix and every unresolved flag), flushed after
every file. `--apply` writes changed files under `--output`; the input is never modified.

---

## Reports reference

| Report | Written by | Contents |
|---|---|---|
| `reports/strip_links-*.json` / `.diff` | `strip-links` | per-file change notes + unified diff |
| `reports/interlink-*.json` | `interlink` | per-source exam/topic/audience, links, warnings |
| `reports/review-*.json` | `review-interlinks` | drops, promotions, quiz moves, redo results |
| `reports/rating-*.md` / `.json` | `rate-blogs` | combined grade report + summary |
| `reports/correct-*.diff` / `.json` | `correct-blogs` | per-file notes, warnings, sources + unified diff |
| `reports/rating-*.md` / `.json` | `correct-blogs --review` | post-correction rating of the corrected posts |
| `reports/human-review-*.md` | `correct-blogs --review` | flagged posts with the specific issues to fix |
| `reports/frontmatter-*.diff` / `.json` / `.md` | `fix-frontmatter` | field changes, review items + diff and changelog |
| `reports/factcheck-*.diff` / `.json` / `.md` | `check-facts` | exam findings (applied + unresolved) + diff and human review |

Reports are written atomically (temp file + rename) with microsecond-unique names, so
concurrent runs never collide or corrupt each other. `rating` and `correct` reports are
flushed after every completed file, so an interrupted run keeps its partial results.

---

## Repo layout

```
blog/                       # source content (MDX/markdown), INPUT — never mutated in place
input/                      # interlinks.json, content_inventory.json, guidelines
facts/                      # verified per-exam fact JSON (source of truth for check-facts)
prompts/                    # external rubric prompts (YAML frontmatter + body)
src/contentos/
  agents/link_stripper.py       # Agent 1 — deterministic link stripping
  agents/interlinker.py         # Agent 2 — LLM link selection + code guardrails
  agents/interlink_reviewer.py  # Agent 3 — deterministic mapping repair
  agents/rater.py               # Agent 4 — rubric grading A+ to F
  agents/corrector.py           # Agent 5 — deterministic + LLM repairs
  agents/frontmatter.py         # Agent 6 — frontmatter schema validator/corrector
  agents/factchecker.py         # Agent 7 — verify content against the fact files
  core/loaders.py               # frontmatter-safe load, file discovery
  core/inventory.py             # content_inventory.json load + lookup
  core/pipeline.py              # runners: load → agent → report → write
  core/reporting.py             # diff + JSON + markdown reports
  core/llm.py                   # Gemini + DeepSeek factories
  cli.py                        # typer commands
output/                     # cleaned files / generated link mapping (apply mode)
reports/                    # per-run reports
```

## Tests

```bash
pytest -q
ruff check src tests
```
