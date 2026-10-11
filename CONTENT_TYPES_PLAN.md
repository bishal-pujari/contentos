# contentos — Multi-Content-Type Implementation Plan

> Status: **Phase 0 and Phase 1 complete; Phase 2 (learn) not started.**
> Blogs, study plans, roadmaps and exam FAQ are live in production; the learn family ships later.

## 1. Goal

Make every contentos agent configurable for content types beyond blog posts, via **external
profiles** (one manifest + per-type prompt files), so the same pipeline can rate, correct,
validate frontmatter and fact-check:

- `blogs/` — live
- `study-plans/` — live
- `roadmaps/` — live
- `exam-faq/` — live
- learn family (later): `concept`, `eli10`, `blueprint` under `learn/<exam>/<subtype>/`

## 2. Locked decisions

1. **Config lives in the prompt folder**: `prompts/content_types.json` (manifest), not `input/`.
2. **Learn = a family with three subtypes**: `concept`, `eli10`, `blueprint`. Real layout is
   `learn/<exam>/concept|eli10|blueprint/`; the repo currently has `learn/concept` (no exam
   folder), so both layouts must resolve.
3. **Code-fence protection is global**: fenced ``` / ~~~ blocks and inline `` `code` `` are never
   matched or rewritten by any agent (stripper, corrector, fact checker, SEO, vendor/rename).
4. **`eli10` frontmatter is generated** and tied to its concept. Concept and eli10 share the same
   slug (filenames match apart from the numeric prefix). The link field reuses the blueprint's
   **`parentConcept`** name; its value is the shared concept slug. Not every concept has a blueprint.
5. **Blueprint DB-sourced fields are excluded from correction**: `exam_id`, `content_domain`,
   `learning_order`, `is_anchor`, `parentConcept`. The blueprint frontmatter agent **only touches
   the SEO fields** (`title`, `seoTitle`, `seoDescription`). The owner validates the DB fields.
6. **Structure enforcement is rubric-only** (no deterministic structure gate).
7. **`strip-links` is skipped for learn** (learning order drives navigation; sources/URLs must survive).
8. **Learn runs per subfolder**: `--input learn/<exam>/concept --content-type concept` (predictable).
9. **Grounding is forced on for every learn subtype** (`ground: always`): check against the fact
   files **and** web search. **Fact files supersede** web search and any earlier agent edits.
10. **Code correctness is a future separate agent**; for now the rubric only checks "no obvious issues".
11. **Priority**: Phase 1 (live families) first; learn (Phase 2) later.

## 3. Content model & folder resolution

```
blogs/                              -> blog
study-plans/                        -> study-plan
roadmaps/                           -> roadmap
exam-faq/                           -> exam-faq
learn/<exam>/concept/               -> concept    (family: learn)
learn/<exam>/eli10/                 -> eli10
learn/<exam>/blueprint/             -> blueprint
learn/concept/ (current, no exam)   -> concept    (exam resolved from frontmatter/facts)
```

Resolution rules (used when `--content-type` is omitted):

1. Explicit `--content-type` wins.
2. The **leaf folder name** selects the type (`concept`, `eli10`, `blueprint`, `blogs`,
   `study-plans`, `roadmaps`, `exam-faq`).
3. For learn types, the **parent folder** (if it is not `learn`) is the **exam slug**; fall back to
   frontmatter (`categories[0]`, `exam_id`) or fact-registry matching.

## 4. Configuration — `prompts/content_types.json`

A single manifest. Agents load it; explicit CLI flags still override individual prompt paths.

```jsonc
{
  "version": 1,
  "families": {
    "learn": { "root": "learn", "layout": "<exam>/<subtype>", "subtypes": ["concept", "eli10", "blueprint"] }
  },
  "types": {
    "blog": {
      "dir": "blogs",
      "rating_prompt": "prompts/examos-blog-rating.md",
      "corrections_prompt": "prompts/examos-blog-corrections.md",
      "frontmatter_prompt": "prompts/examos-blog-frontmatter.md",
      "facts_prompt": "prompts/examos-content-facts.md",
      "seo_prompt": "prompts/examos_blog_seo_rewritre.md",
      "exam_binding": ["examCodes"],
      "ground": "auto",
      "frontmatter": "normalize",
      "faq": { "applies": true, "heading": "## Frequently Asked Questions (FAQ)", "questions": 4, "question_style": "h3" },
      "strip_links": true,
      "vendor_contamination": true,
      "nested_preserve": []
    },
    "study-plan": {
      "dir": "study-plans",
      "rating_prompt": "prompts/examos-study-plan-rating.md",
      "corrections_prompt": "prompts/examos-study-plan-corrections.md",
      "frontmatter_prompt": "prompts/examos-study-plan-frontmatter.md",
      "facts_prompt": "prompts/examos-content-facts.md",
      "seo_prompt": "prompts/examos-study-plan-seo.md",
      "exam_binding": ["examCode"],
      "ground": "always",
      "frontmatter": "normalize",
      "faq": { "applies": true, "heading": "## Frequently Asked Questions (FAQ)", "questions": 4, "question_style": "h3" },
      "strip_links": true,
      "vendor_contamination": true,
      "nested_preserve": []
    },
    "roadmap": {
      "dir": "roadmaps",
      "rating_prompt": "prompts/examos-roadmap-rating.md",
      "corrections_prompt": "prompts/examos-roadmap-corrections.md",
      "frontmatter_prompt": "prompts/examos-roadmap-frontmatter.md",
      "facts_prompt": "prompts/examos-content-facts.md",
      "exam_binding": [],
      "ground": "auto",
      "frontmatter": "normalize",
      "faq": { "applies": false },
      "strip_links": true,
      "vendor_contamination": false,
      "nested_preserve": ["skills", "certificationPath", "milestones", "steps", "finalCerts",
                          "timeline", "honestTimeline", "finalSectionTitle", "finalSectionBody"]
    },
    "exam-faq": {
      "dir": "exam-faq",
      "rating_prompt": "prompts/examos-exam-faq-rating.md",
      "corrections_prompt": "prompts/examos-exam-faq-corrections.md",
      "frontmatter_prompt": null,
      "facts_prompt": "prompts/examos-content-facts.md",
      "exam_binding": ["filename_slug"],
      "ground": "always",
      "frontmatter": "skip",
      "faq": { "applies": false, "document_is_faq": true },
      "strip_links": false,
      "vendor_contamination": true,
      "nested_preserve": []
    },
    "concept":  { "dir": "concept", "family": "learn", "ground": "always", "exam_binding": ["exam_folder", "categories"],
                  "rating_prompt": "prompts/examos-concept-rating.md",
                  "corrections_prompt": "prompts/examos-concept-corrections.md",
                  "frontmatter_prompt": "prompts/examos-concept-frontmatter.md",
                  "facts_prompt": "prompts/examos-content-facts.md",
                  "faq": { "applies": false }, "frontmatter": "normalize",
                  "strip_links": false, "vendor_contamination": false, "nested_preserve": [] },
    "eli10":    { "dir": "eli10", "family": "learn", "ground": "always", "exam_binding": ["exam_folder", "categories"],
                  "rating_prompt": "prompts/examos-eli10-rating.md",
                  "corrections_prompt": "prompts/examos-eli10-corrections.md",
                  "frontmatter_prompt": "prompts/examos-eli10-frontmatter.md",
                  "facts_prompt": "prompts/examos-content-facts.md",
                  "faq": { "applies": false }, "frontmatter": "generate",
                  "link_field": "parentConcept", "link_source": "matching_concept_slug",
                  "strip_links": false, "vendor_contamination": false, "nested_preserve": [] },
    "blueprint":{ "dir": "blueprint", "family": "learn", "ground": "always", "exam_binding": ["exam_folder", "exam_id"],
                  "rating_prompt": "prompts/examos-blueprint-rating.md",
                  "corrections_prompt": "prompts/examos-blueprint-corrections.md",
                  "frontmatter_prompt": "prompts/examos-blueprint-frontmatter.md",
                  "facts_prompt": "prompts/examos-content-facts.md",
                  "faq": { "applies": false }, "frontmatter": "seo_only",
                  "exclude_fields": ["exam_id", "content_domain", "learning_order", "is_anchor", "parentConcept"],
                  "strip_links": false, "vendor_contamination": false, "nested_preserve": [] }
  }
}
```

### Flag semantics
- `frontmatter`: `normalize` (schema check/correct) · `generate` (add when missing) · `skip`
  (never touch) · `seo_only` (only title/seoTitle/seoDescription).
- `equate ground`: `auto` (existing heuristic) · `always` (force grounding/search).
- `faq.applies`: whether the corrector may add/normalize a FAQ section. `document_is_faq` (exam-faq)
  means the whole file is a Q&A list, so no section is added.
- `nested_preserve`: fields re-emitted verbatim (real YAML), never re-ordered/rewritten.

## 5. Phase 0 — shared foundations (safe, no behaviour change) — ✅ done

Delivered: `prompts/content_types.json`, `core/protected.py`, `core/profiles.py`, the
frontmatter nested-emitter fix (+ `nested_preserve`) and the `--content-type` /
`--profiles` CLI flags on `strip-links`, `interlink`, `rate-blogs`, `correct-blogs`,
`fix-frontmatter` and `check-facts` (defaults now `blogs`). Prompt selection still uses
the explicit `--prompt` defaults; Phase 1 wires the profile through the agents.

### 5.1 `src/contentos/core/protected.py` (new)
- `protected_spans(text, kinds=("fenced","inline","urls"))` → spans of fenced blocks (` ``` ` / `~~~`),
  inline code (`` `…` ``), and markdown link targets/URLs.
- `mask(text, kinds)` → equal-length text with protected spans blanked (for scanning).
- `replace_guarded(text, find, replacement, case_insensitive=False, kinds=...)` → replaces only
  outside protected spans.
- Absorbs the fact checker's existing `_url_spans` / `_mask_urls` (fact checker imports from here).

### 5.2 `src/contentos/core/profiles.py` (new)
- `ContentProfile` dataclass and `ContentTypeManifest`.
- `load_manifest(path="prompts/content_types.json")`.
- `resolve_profile(input_dir, content_type=None, manifest=...)` implementing §3 resolution.
- `exam_binding_value(profile, doc, path, facts_registry)` → exam slug/code (parent folder,
  `examCode`/`examCodes`/`exam_id`, filename slug, `categories`).

### 5.3 Frontmatter nested-field fix (`agents/frontmatter.py`)
- Replace the ad-hoc `_emit_unknown` scalar formatting with a real YAML dump for complex values
  (`yaml.safe_dump(value, default_flow_style=False, sort_keys=False, allow_unicode=True)`),
  indented to the field. Known list fields containing dicts use the same path.
- `nested_preserve` fields are emitted verbatim (captured from the source block).

### 5.4 CLI / defaults
- Add `--content-type` and `--profiles prompts/content_types.json` to `rate-blogs`, `correct-blogs`,
  `fix-frontmatter`, `check-facts`, `strip-links`, `interlink`.
- Fix the folder rename: defaults `--input blog` → `blogs`.
- Keep existing `--prompt` / `--rating-prompt` overrides.

## 6. Phase 1 — live families (blog, study-plan, roadmap, exam-faq) — ✅ done

Delivered: the manifest now drives every agent. `--content-type` (or folder inference) selects
the per-type rating/correction/frontmatter/SEO prompt, the grounding mode (`ground: always`
forces the first grounded pass for study-plan/exam-faq) and the FAQ behaviour
(`faq.applies:false` means no FAQ section is ever added). New prompts:
`examos-{study-plan,roadmap,exam-faq}-rating.md`, `...-corrections.md`,
`examos-{study-plan,roadmap}-frontmatter.md` and `examos-{study-plan,roadmap}-seo.md`.
Frontmatter is skipped for exam-faq, and roadmaps round-trip their nested YAML verbatim
(`nested_preserve`); study plans use the singular `examCode` and a string `passingScore`.
Corrector/check-facts replacements are guarded by `core/protected.py`, so code fences and
inline code are never touched. `check-facts` binds by `examCode` / `examCodes` / `exam_id`,
the filename slug (exam-faq) or the learn exam folder, and allows `vendor_contamination:false`
for roadmaps. `strip-links` is skipped when `strip_links:false`.

### Rater (`agents/rater.py` + `core/pipeline.py` + `cli.py`)
- Resolve rubric from the profile (`--prompt` overrides).
- Profile-driven grounding: `study-plan`, `exam-faq` = always; `blog`/`roadmap` = auto.
- New rubrics: `examos-study-plan-rating.md`, `examos-roadmap-rating.md`, `examos-exam-faq-rating.md`.
- No code change to scoring; only prompt selection + grounding flag.

### Corrector (`agents/corrector.py` + pipeline/cli)
- Load FAQ config from the profile. `roadmap`/`exam-faq` → `faq.applies:false` so **no FAQ is added**.
- All phrase/fact/SEO-opening replacements route through `protected.replace_guarded`; fact/claim
  scanning uses `protected.mask`. **Code fences are never touched.**
- Per-type correction prompts; keep SEO off by default (fix-frontmatter owns SEO).
- Passes gated by profile where relevant (facts on; ExamOS auto when "examos" appears).

### Frontmatter (`agents/frontmatter.py` + pipeline/cli)
- `exam-faq` → `frontmatter: skip` (never add a block).
- `study-plan` schema: `examCode` singular, `provider`, `description`, `estimatedHours`,
  `passingScore` (string, e.g. `"700 / 1000"`), `targetAudience`, SEO fields, categories, order.
- `roadmap` schema: SEO/order/categories + `provider`, `description`, `cardSummary`, `audience`,
  `totalTime`; nested fields preserved verbatim.
- Move hardcoded `STUDY_PLAN_FIELDS` / `type` consts / category+author lists out of code into the
  schema prompts (markdown or YAML).

### Fact checker (`agents/factchecker.py` + pipeline/cli)
- Read `examCode` (singular) **and** `examCodes`; add filename-slug → fact-slug binding.
- Use `protected` masking/replacement (code fences + URLs).
- `vendor_contamination` off for roadmap (multi-cloud by nature).

### Stripper / interlink
- `strip_links:false` for exam-faq (recommended) and learn.
- Inventory already has `roadmap`/`study-plan`; no change.

## 7. Phase 2 — learn family (concept, eli10, blueprint)

Ships after Phase 1.

### Purpose-built prompts (rubrics teach the rigor; no deterministic structure gate)
- `examos-concept-rating.md` — factual/grounded accuracy, section completeness & order
  (Overview → TL;DR → Problem → How It Works → Core Elements → Step-by-Step → Key Takeaways →
  Future Reading), clarity, callout/trap quality.
- `examos-eli10-rating.md` — plain-language accuracy, ELI10 level, section completeness
  (What is happening? → Example → Real tech example → Takeaway).
- `examos-blueprint-rating.md` — factual accuracy, code correctness "at a glance", prerequisites,
  numbered sections, verification, gotchas, **sources table present**, no obvious issues.
- `examos-concept-corrections.md`, `examos-eli10-corrections.md`, `examos-blueprint-corrections.md`
  (shared voice/AI-slop rules; **never edit inside code**).
- `examos-concept-frontmatter.md`, `examos-eli10-frontmatter.md`, `examos-blueprint-frontmatter.md`.

### Frontmatter behaviour
- `concept`: normalize `type:"learning"` const + slug/date/title/seo/order/categories.
- `eli10` (`frontmatter: generate`): add `slug` (matches the concept slug), `type:"eli10"`, order from
  the filename prefix, `categories` from the exam, and **`parentConcept`** = the matching concept slug
  (matched by filename base). Flag for review.
- `blueprint` (`frontmatter: seo_only`): only rewrite `title`/`seoTitle`/`seoDescription`; never touch
  `exam_id`, `content_domain`, `learning_order`, `is_anchor`, `parentConcept` (DB-owned).
- Exam binding: parent folder (e.g., `claude-certified-architect-foundations-cca-f`) → fact file
  `facts/claude-certified-architect-foundations-cca-f.json`; fall back to `categories[0]` (`cca-f`).

### Grounding & facts
- `ground: always` for all learn subtypes; the rater runs grounded on the first pass.
- Fact checker links via parent folder / `exam_id` / categories; **facts supersede** web search and
  previous edits; code fences are protected so model IDs / SDK snippets are never rewritten.

### Stripper
- Skipped for the whole learn family (blueprint **Sources** table and URLs survive).

## 8. Phase 3 — future
- Separate **code-correctness agent** (validates SDK/model/API usage).
- Add learn entries to `content_inventory.json` so learn content can be interlinked.

## 9. File change summary

New:
- `prompts/content_types.json`
- `src/contentos/core/protected.py`
- `src/contentos/core/profiles.py`
- `prompts/examos-{study-plan,roadmap,exam-faq}-rating.md`
- `prompts/examos-{study-plan,roadmap,exam-faq}-corrections.md`
- `prompts/examos-{study-plan,roadmap}-frontmatter.md`
- Phase 2: `prompts/examos-{concept,eli10,blueprint}-{rating,corrections,frontmatter}.md`

Changed:
- `src/contentos/agents/{rater,corrector,frontmatter,factchecker,link_stripper}.py`
- `src/contentos/core/{pipeline,reporting}.py`
- `src/contentos/cli.py`
- `AGENTS.md`, `README.md`, `PLAN.md`
- Tests under `tests/`

## 10. Test matrix

- Profile resolution: folder inference (flat and `learn/<exam>/<subtype>`), exam binding per type.
- Protected regions: fenced/inline code never matched or rewritten (corrector + fact checker +
  stripper); URLs survive; idempotence.
- Frontmatter: nested YAML round-trips for roadmaps; `skip` for exam-faq; `generate` for eli10 with
  `parentConcept`; `seo_only` for blueprint leaves DB fields untouched.
- Corrector: no FAQ injected for roadmap/exam-faq/learn; code blocks unchanged.
- Fact checker: `examCode` singular and filename-slug binding; vendor-contamination off for roadmaps;
  facts supersede; code fences protected.
- Rater: per-type rubric selection and forced grounding for study-plan/exam-faq/learn.
- One end-to-end dry-run per type on the sample folders.

## 11. Risks & non-goals

- **Rubric-only structure** means structural defects are judged by the LLM; residual defects are
  possible (accepted).
- **Blueprint DB fields** are intentionally out of scope; the owner validates them.
- **Roadmap nested YAML** must never be re-serialized lossily — the Phase 0 emitter fix is a hard
  prerequisite for running `fix-frontmatter` on roadmaps.
- **Non-goals now**: code-correctness agent, learn interlinking, mixed-folder single runs.
