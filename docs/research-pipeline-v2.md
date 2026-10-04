# Research Pipeline V2 — evidence quality, source extraction & Scrapling

Branch: `cloud/research-pipeline-v2` (base `test` @ `65b9fb5`). V2 is a new package
`apps/api/clipforge/research_v2/` behind the existing seam `research.research_topic`; the
V1 path stays as the isolated fallback (`RESEARCH_PIPELINE=v1` selects it explicitly).

* Part 1 — Phase 0 audit of the existing research path (written before implementation).
* Part 2 — V2 design.
* Part 3 — Evaluation (offline harness, real-project comparison, network retest, Real-Mac steps).

---

## Part 1 — Phase 0 audit

### 1.1 Trace of the current path (V1)

| Stage | Where | What it does |
|---|---|---|
| Entry | `pipeline.build_initial_state` → `_build_initial_state` | `intent.research_required` (research `on`, or `auto` and not fiction) → `research_topic(query, language, settings)` |
| Query | `question_intent.research_query` or the raw prompt; `research._query_from_prompt` | Leading question word stripped ("Warum bleibt Essen …" → "bleibt Essen in der Mikrowelle in der Mitte kalt") |
| Search providers | `research.py` | **Brave Web Search** (`count=5`, timeout 6 s) when a key exists, else **Wikipedia** (`srlimit=1`, `exintro`) |
| HTTP retrieval | — | **None.** No page is ever fetched. |
| Extraction | `research._sentences` (Wikipedia intro: first 4 sentences ≥ 5 words); Brave: the `description` snippet itself | |
| Fact objects | `research._fact_records` | `confidence` fixed **0.78**; `importance = 0.95 - rank*0.1`; first three by search rank are `MUST_KNOW`; `sources` = the one result the snippet came from; `verification="source_snippet"` |
| Cleaning | `narration.clean_research_claim` (the "research boundary") | Drops page chrome, cut-off sentences, attribution scaffolding |
| Planner | `ai.plan_with_openai(evidence=[claims])` | Story arc, payoff plan, `answers_why`; when research returned no facts the **planner's own facts** are used, with `source_url` it names marked `source_attributed` |
| Downstream | `novelty.safe_novelty_plan` → `format_intelligence.plan_format` → `story_arc.safe_story_arc` (question contract, explanation spine) → `script_writer` (facts ≤ 10, only `supported`/`source_attributed` citable) → `triple_hook` (numbers only from sourced facts) → `novelty.prune_redundant_information` → `readiness.content_readiness` | |
| Retry | `pipeline.build_initial_state`, `MAX_RESEARCH_RETRIES = 1`, `research_retry_query` | One more full build with `question + "Ursache Mechanismus warum" + condition terms + missing` when readiness says `research_required` |
| Gate | `factual_ready = not research_required or bool(facts and sources)` → `render.status = blocked_by_research`; `readiness` → `ScriptNotReady` before TTS/render | |
| Caching / dedupe / freshness / source limits | — | None at research level (novelty dedupes claims later) |
| Editor "add a fact" | `pipeline` edit path → `research_topic(prompt + instruction)` | First new snippet appended |
| Persisted | `state.research = {required, questions, status, provider, error, sources, attempts}` | No dates, types, excerpts or diagnostics |

### 1.2 Existing safeguards (kept, not duplicated)

Core-question contract (`story_arc.question_contract`), answer sufficiency
(`novelty._answer_sufficiency`), explanation spine (`story_arc.explanation_spine`, status
`missing_mechanism`), unsupported causal hook protection (`verbal_hook.ungrounded_cause` in
`readiness`), one bounded mechanism retry (`MAX_RESEARCH_RETRIES = 1`), meta-failure narration
blocking (`verbal_hook.narrates_failure`, writer requirement "never write a sentence about the
facts"), insufficient answers stop production (`readiness` → `ScriptNotReady`,
`blocked_by_research`). V2 feeds these systems better evidence; it does not re-implement any.

### 1.3 Root causes — proven from code and saved runs

| Failure | Cause (code) | Evidence |
|---|---|---|
| **Shallow / generic answers** | Wikipedia fallback reads only the article **intro** (`exintro=1`) of the **single** top hit (`srlimit=1`), first 4 sentences. Mechanism sections are never read. | `research.py` V1 |
| **Snippets used as facts** | Brave `description` (≈ 1–2 sentences, often cut off) *is* the fact; the page is never retrieved. | `_fact_records(..., per_claim=True)` |
| **Search ranking = authority** | `importance = 0.95 - index*0.1`, first three ranks `MUST_KNOW`. | `_fact_records` |
| **No source quality** | Sources are `{label, url}`; a forum thread, an SEO list and a university page are equal. | Real islands run (`tests/test_real_runtime_islands.py`, research verbatim): fact 1 is a forum thread ("Beitrag archiviert … Downvotes"), fact 5 an SEO list with "Foto: Getty Images". |
| **Answer fragments without context / page chrome** | Snippets end mid-sentence; `clean_research_claim` must drop them, leaving fewer facts. | Islands fact 5 "Das wird deutlich, wenn man das." ; fact 2 "Aber bei weitem nicht jede Insel lädt auch wirklich zum Baden ein." became the spoken hook (`tests/test_hook_runtime_regression.py`). |
| **Weak causal explanations** | The first query is the prompt minus its question word — no mechanism-directed query; a snippet contains a cause only by chance. The retry searches again with the same snippet method. | `pipeline.research_retry_query`, `MAX_RESEARCH_RETRIES` |
| **Wikipedia misses for sentence-shaped queries** | Full-text search needs every word: "bleibt Essen in der Mikrowelle in der Mitte kalt" returns nothing → `IndexError` → `unavailable`. | V1 `search.json()["query"]["search"][0]` |
| **Duplicated / secondary-source chains** | No corroboration model: each V1 fact has exactly one source, so `novelty`'s `source_count >= 2` paths (distinctive/surprising detail) are unreachable; copies of one press release would count as different pages. | `novelty.build_novelty_plan` |
| **Stale claims** | No publication/update date, Brave `page_age` ignored; `current_explainer` intent never reaches research. | V1 |
| **Unsupported confident facts** | With empty research, planner facts carrying a model-named `source_url` become `source_attributed` — an LLM-invented URL counted as evidence. | `pipeline._build_initial_state` (planner-fact branch) |
| **No traceability / diagnostics** | Facts keep only a URL; no excerpt, date, type, or why a source was used. Every regression fixture of a real run says "research facts were not kept with the runs". | test docstrings (`test_answer_gate.py`, `test_explanation_spine.py`, …) |
| **Failure isolation** | Any Brave error aborts research (and is reported as provider `wikipedia`). | V1 `except` branch |

Saved projects: no project database exists in this checkout/container; the only verbatim
persisted research of a real run is the islands run (above). See 3.3.

### 1.4 KEEP / IMPROVE / REPLACE / REMOVE

| Item | Decision | Note |
|---|---|---|
| `research_topic` seam + `ResearchResult` | **KEEP** (+ `package`, `diagnostics` fields) | 30+ tests patch this seam |
| Brave Web Search, Wikipedia | **KEEP** as discovery providers | no paid providers added |
| `clean_research_claim`, `question_intent.research_query` | **KEEP** | research boundary unchanged |
| Question contract, answer sufficiency, explanation spine, causal-hook protection, meta-failure blocking, readiness gate | **KEEP** | V2 improves their inputs |
| One bounded retry | **IMPROVE** | still exactly one; now with a `focus` (`mechanism` or `broaden`) and V2's mechanism sub-question from the start |
| Planner input | **IMPROVE** | optional `research_brief` (which evidence is the answer / mechanism / misconception / corroborated) |
| Readiness | **IMPROVE** | `research_insufficient` blocks when no retrieved source contains a direct answer |
| Planner facts with model-named URLs | **IMPROVE** | attributable only if research retrieved that URL, else `unverified_model_synthesis` |
| Snippet-as-fact, Wikipedia intro top-1, fixed 0.78 confidence, rank-based importance | **REPLACE** | retrieved pages → evidence units → corroborated claims |
| V1 code path | **KEEP as fallback** | runs only if V2 crashes or `RESEARCH_PIPELINE=v1` |
| Rank-as-authority, LLM-URL-as-attribution | **REMOVE** (behaviour) | |

---

## Part 2 — V2 design

```
QUESTION ─► DECOMPOSITION (core [+ mechanism] [+ detail/misconception/current], ≤ 4)
         ─► ROUTING (domain → preferred source types)
         ─► DISCOVERY (Brave per sub-question; Wikipedia search; cited high-authority links)
         ─► CHEAP PREFILTER (snippet relevance band, predicted type, ≤ 2 per domain, sub-question coverage)
         ─► RETRIEVAL (cache → encyclopedia API → HTTP [robots, access control] → optional browser)
         ─► EXTRACTION (main content + metadata; Scrapling parser, stdlib fallback)
         ─► EVIDENCE UNITS (sentence + context, kind, relevance gate, sub-question)
         ─► FRESHNESS ─► INDEPENDENCE CLUSTERS ─► CLAIM GROUPS ─► CONTRADICTIONS
         ─► CLAIM SELECTION (deterministic; or LLM synthesis validated against cited evidence)
         ─► RESEARCH PACKAGE + legacy facts + diagnostics
```

| Module | Responsibility |
|---|---|
| `routing.py` | domain (`science`, `health`, `history`, `technology`, `current_events`, `everyday`) from grammar/vocabulary cues → ordered **source types** (never sites); time sensitivity; whether first-party sources apply |
| `quality.py` | transparent `source_type` + `authority` tier + `reasons` |
| `discovery.py` | providers; a hit is a lead, never evidence |
| `retrieval.py` | fetch fallback order, robots.txt, access control, retries, cache, deadline |
| `extraction.py` | main content, dates, author/organisation, headings, tables |
| `evidence.py` | evidence units and kinds |
| `corroboration.py` | independence, claim groups, contradictions, freshness |
| `package.py` | roles, verification, sufficiency, package, legacy facts, validation of synthesis |
| `synthesis.py` | optional LLM decomposition/synthesis (worker model) with deterministic fallbacks |
| `service.py` | orchestration, budget, diagnostics |

### 2.1 Scrapling — USED (parser + optional dynamic render), not as the default fetcher

`scrapling` (0.4.x, base install = parser only: lxml + cssselect) is a core dependency and is
used for **extraction**: DOM-aware main-container choice and boilerplate ancestry (nav, cookie
banners, related links, comments, footers, captions). It materially improves extraction over
the stdlib scanner on real pages with nested layout; `extraction.py` keeps a stdlib fallback,
so a missing/broken Scrapling never stops research (tested).

Scrapling's `Fetcher` is **not** used for normal retrieval: its advantage over httpx is
browser TLS/header impersonation (curl_cffi), i.e. anti-bot behaviour, which must not be the
default. `StealthyFetcher` is never used. `DynamicFetcher` (plain headless Chromium) renders a
page only when (a) the page is detected as JS-only, (b) `RESEARCH_BROWSER_FETCH=true`, (c) the
optional extra `scrapling[fetchers]` + Playwright are installed and (d) the per-run dynamic
budget (default 1) allows it. Off by default.

### 2.2 Fetch fallback order

1. extraction cache (extracted page only, never raw HTML; 7 days evergreen, 2 h current events);
2. Wikipedia article URLs → MediaWiki API plain-text extract (no scraping; sections; references cut);
3. HTTP GET via httpx: honest User-Agent `ClipForgeResearch/2.0`, robots.txt (RFC 9309: 4xx = allowed,
   5xx = disallowed, unreachable = host failed), ≤ 5 redirects, ≤ 1.5 MB, HTML/XML/plain only,
   timeout 8 s, at most one retry for timeouts/5xx (global retry budget 2);
4. JS-only page → Scrapling `DynamicFetcher` only under 2.1's conditions;
5. retrieval failed → the search snippet becomes *snippet-basis* evidence (`source_snippet`,
   confidence 0.7, gap `core_answer_snippet_only`) — never treated like full text.

401/402/403/407/451 → `access_denied`, never bypassed. 429 → `rate_limited` and the host is
skipped for the rest of the run. Every failure is an outcome record; one site cannot break
research (thread-pool isolation, per-host circuit breaker, wall-clock deadline 45 s).

### 2.3 Source routing

| Domain | Preferred types (first = most preferred) |
|---|---|
| science | primary_research, government, academic, institutional, reference, specialist_secondary, journalism |
| health | government, primary_research, academic, institutional, reference, journalism, specialist_secondary |
| history | institutional (archives/museums), academic, government, primary_research, reference, journalism |
| technology | first_party, academic, institutional, government, reference, specialist_secondary, journalism |
| current_events | journalism, government, first_party, institutional, academic, reference |
| everyday | academic, institutional, government, primary_research, reference, specialist_secondary, journalism |

Routing orders *relevant* sources only: every selection key starts with a relevance band, so an
authoritative page that does not talk about the question never wins (tested with a government
Jupiter page against a microwave question). First-party is allowed only for technology/current
events (a "mars.com" brand page is not an authority on the planet).

### 2.4 Source quality model (no single trust score)

`source_type` ∈ primary_research · government · academic · institutional · first_party ·
reference · journalism · specialist_secondary · generic_secondary · user_generated ·
low_quality · unknown, with `authority` high / medium / low / unknown and the `reasons`.
Signals are generic: scholarly citation meta (`citation_doi`, `citation_journal_title`), host
shape (`.gov`, `.bund.de`, `.edu`, `.ac.xx`, `uni-`, `museum`, `archiv`, `institut`),
schema.org types (GovernmentOrganization, Museum, NewsArticle …), SEO signals (listicle title,
affiliate/ad markers, content-farm phrasing, thin main text). Unknown sources are not banned;
low-quality/user-generated evidence is used only when nothing better exists, and the package
then carries the gap `core_answer_low_authority_source`.

### 2.5 Evidence model

Evidence unit: `id`, verbatim `text` (one sentence; the preceding sentence is included when the
sentence starts with an anaphor such as "Dies…", "Trotz dieser…"), `excerpt` (≤ 400 chars),
`source_id`, `sub_question`, `kind` (misconception · mechanism · number · date · comparison ·
caveat · definition · observation), `relevance`, matched terms, `basis` (full_text/snippet),
normalised `numbers`, `time_sensitive`. Source record: URL, domain, title, organisation,
author, published/updated dates, `fetched_at`, type, authority, reasons, independence cluster,
retrieval status. Raw pages are never persisted; the package keeps only the used evidence.

### 2.6 Claim → evidence traceability

Every legacy fact handed to generation carries `research_key`, `research_role`,
`evidence_ids`, `independent_sources`, `verification` and per-cluster `sources` (URL, type,
authority, dates). After the pipeline re-numbers facts, `link_package_facts` writes the final
`fact_id` into every package ref, so `core_answer`, each mechanism step, numbers/dates,
misconceptions and caveats resolve to a fact *and* to evidence excerpts *and* to sources.
Verbatim evidence is traceable by construction; LLM-synthesised claims must pass
`package.validate_synthesized`: known evidence IDs, no number absent from the cited evidence,
≥ 60 % of content words found in the cited text, a causal claim only from causal evidence.
Rejected claims are listed in `rejected_claims` and never reach the writer. Planner facts with
a model-named URL are `unverified_model_synthesis` unless research retrieved that URL.

### 2.7 Multi-source corroboration & source independence

Claims are grouped across sources (content overlap ≥ 0.4 with ≥ 4 shared words, compatible
numbers, same polarity; sentences of one page never merge). Support = number of independent
**clusters**: sources are clustered by registrable domain, canonical URL, near-duplicate text
(5-word shingles ≥ 0.3 → syndicated copy) and shared wire attribution ("(dpa)", "laut
Pressemitteilung"). `verification`: `supported` = ≥ 2 independent clusters, or one
high-authority full-text source; `source_attributed` = one medium/low source; `source_snippet` =
snippet basis. Confidence 0.9 (≥ 2 clusters) / 0.85 (one high) / 0.78 (one medium) / 0.62
(one low or unknown-authority) / 0.7 (snippet). A fact's `sources` lists one source per cluster, so copies never
inflate counts downstream (`novelty` reads `source_count` from it).

### 2.8 Causal / mechanism research and question decomposition

A why/how question (`story_arc.is_explanatory_question`) gets a mechanism sub-question **in the
first pass** (deterministic: subject + "Ursache Erklärung wie funktioniert"; with OpenAI the
worker model writes ≤ 4 necessary sub-questions incl. English queries for international primary
sources). The package separates **what happens** (observation), **why it happens** (mechanism
evidence with cause grammar), **how it works** (process mechanism evidence) and **what the
viewer should understand** (validated takeaway, else the core answer). Without mechanism
evidence the package says `missing_mechanism` (gap `no_mechanism_evidence`); the existing spine
and answer-sufficiency checks then trigger the existing **single** retry, now with
`focus=mechanism`. No new retries were added.

### 2.9 Answer sufficiency

`package.sufficiency`: direct answer · mechanism (why/how only) · supporting detail ·
misconception/caveat (optional) · payoff (≥ 2 claims). Status `sufficient` / `partial` /
`missing_mechanism` / `insufficient`. **Insufficient** (no relevant claim at all) hands *no*
facts to generation: `factual_ready` is false (`blocked_by_research`), readiness blocks with
`research_insufficient`, and the one retry broadens (`focus=broaden`: subject words only).
Nothing is narrated about the failure (status block only, never rendered).

### 2.10 Freshness

Each evidence unit is checked against its source date (updated, else published).
Current-events questions: ≤ 45 days, undated sources cannot support claims. Claims with time
words ("derzeit", "aktuell", "latest", "record") in evergreen questions: ≤ 3 years. Everything
else is `evergreen` — never penalised for an old page. Rejected units are listed with
`stale_for_time_sensitive_claim` / `undated_for_time_sensitive_claim`.

### 2.11 Contradictions

Same-topic claim groups from different clusters disagree when their numbers differ by > 20 %
(numeric) or one negates what the other affirms (polarity). The side with ≥ 1 more independent
cluster (and no worse authority) wins (`consensus`), the other is `disputed`; otherwise both are
`conflicting`, withheld, and the gap `unresolved_contradiction` is reported.

### 2.12 Budget (per research run; the one retry is a second run)

| Resource | Default | Setting |
|---|---|---|
| discovery searches | 6 | `RESEARCH_MAX_SEARCHES` |
| documents retrieved | 6 (≤ 2 per domain, ≤ 2 cited links) | `RESEARCH_MAX_DOCUMENTS` |
| browser renders | 0 (1 when enabled) | `RESEARCH_BROWSER_FETCH`, `RESEARCH_MAX_BROWSER_FETCHES` |
| LLM calls | 2 (decomposition + synthesis; 0 without OpenAI mode) | `RESEARCH_MAX_LLM_CALLS` |
| HTTP retries | 2 | — |
| fetch timeout / deadline | 8 s / 45 s | `RESEARCH_FETCH_TIMEOUT_SECONDS`, `RESEARCH_DEADLINE_SECONDS` |
| evidence | ≤ 8 units per source, ≤ 28 sent to synthesis, ≤ 8 facts (writer limit 10) | — |

Cheap filtering (snippet relevance, predicted type, domain quota) happens before any fetch.

### 2.13 Research package (`state.research.package`)

`core_answer`, `explanation_spine` {what_happens, why_it_happens[], how_it_works[],
viewer_takeaway, status}, `supporting_facts`, `numbers_dates`, `caveats`, `misconceptions`,
`evidence` (used units only), `source_summary` {used, independent, types, sources},
`contradictions`, `sufficiency`, `confidence`, `gaps`, `rejected_claims`, `route`,
`sub_questions`, `synthesis` mode. Compact diagnostics in `state.research.diagnostics`:
discovery calls and prefilter decisions, retrieval outcomes (status, method, extractor),
failures, every source with type/authority/reasons/cluster/independence note/dates, claim
groups with support and status, mechanism origin (claim → evidence → source types),
contradictions, rejected claims, sufficiency, budget usage.

### 2.14 Downstream contract

Unchanged consumers, better inputs: facts arrive in spine order (core answer, mechanism
steps, observation, numbers, supporting, misconception, caveat) with `MUST_KNOW` for answer and
mechanism, so the deterministic story arc, novelty plan and writer summary see the explanation
spine first; the planner gets `research_brief` (1-based evidence indexes of answer/mechanism/
observation/misconception/caveat/corroborated) as guidance for `primary_answer_index` and
`answers_why`; the writer only receives `supported`/`source_attributed`/`source_snippet` facts
(conflicting/disputed/stale/unsupported never reach it); Triple Hook still takes numbers only
from sourced facts — now numbers that exist in retrieved evidence. Story Intelligence, Triple
Hook and Topic Intelligence code is unchanged.

---

## Part 3 — Evaluation

### 3.1 Offline harness (`tests/test_research_pipeline_v2.py`, `tests/research_v2_support.py`)

A deterministic fake web (`httpx.MockTransport`) with Brave/Wikipedia APIs, robots.txt and
pages. Fixtures and what they prove:

| Fixture | Proof |
|---|---|
| strong primary (university + federal agency) vs **generic SEO article** | authoritative relevant sources win; the SEO page is classified `low_quality` and unused |
| government page about Jupiter for a microwave question | **irrelevant authority does not win** (0 evidence units) |
| three **syndicated copies** of a dpa text | one independence cluster: `independent_sources = 1`, `source_attributed`, conf < 0.85 |
| university + agency agreeing | `independent_sources = 2`, `supported`, conf 0.9 |
| **stale current article** (2019) vs fresh (2026) | stale claim rejected (`stale_for_time_sensitive_claim`), fresh used |
| old page for an evergreen fact | not penalised |
| **conflicting numbers** (3.8 vs 9.5 cm) | recorded as numeric contradiction, both withheld, gap reported |
| **surface fact without mechanism** | `missing_mechanism` + gap; existing retry path takes over |
| **JS-only page** | `js_only` without browser; rendered once via injected dynamic fetcher only when enabled |
| **extraction failure** + dead host | isolated (`extraction_failed`, `network_error`), research still `sufficient` |
| **insufficient evidence** | `insufficient`, no facts, status `unavailable` |
| **numerical claim with evidence** | number → evidence unit → source, `fact_id` linked |
| **unsupported numerical claim** (LLM "90 Sekunden … 12 Grad"), uncited cause, unknown ID | rejected (`number_not_in_evidence`, `no_evidence_cited`, `unknown_evidence_ids`) |
| robots / 402 / 429 / budget / cache / no-Scrapling | respected, bounded, cached as extracted text, stdlib fallback |

`tests/test_research_v2_integration.py` runs the real pipeline: package and fact IDs linked,
insufficient research blocks readiness and broadens exactly once, planner-named URLs are not
research, planner receives the brief, V2 crash → V1 fallback, V1 selectable.

### 3.2 Validation run

See the commit message for the exact numbers of this branch's run (new V2 tests, existing
research/answer/retry/story tests, full backend suite, ruff on changed files, `git diff --check`).

### 3.3 Real project comparison

No project database exists in this container; the only verbatim persisted research of a real
run is "Welches Land hat mehr Inseln – Schweden oder Indonesien?" (Brave, 5 snippets, in
`tests/test_real_runtime_islands.py`). Re-reading it through V2's evidence layer (snippet basis,
because the pages cannot be fetched offline; nothing written back):

| | OLD (V1) | V2 on the same material |
|---|---|---|
| Source quality | 5 × `{label,url}`, no types | each classified; all `unknown` (snippet-only) → confidence `low`, gaps `core_answer_snippet_only`, `core_answer_low_authority_source` |
| Factual depth | 5 snippets, cut off | 5 claims; the forum chrome ("Beitrag archiviert … Downvotes"), "Foto: Getty Images" and the cut fragment "Das wird deutlich, wenn man das." are not evidence |
| Off-question sentence | "Aber bei weitem nicht jede Insel lädt … zum Baden ein." was a fact and became the spoken hook | relevance 0.38, never selected |
| Context | "Trotz dieser imposanten Zahl …" separate from its referent | merged with the preceding sentence |
| Core answer | rank 1 snippet by search order (the "Top fünf" sentence) | "Schweden ist das Land mit den meisten Inseln weltweit – stolze 267.570 …" (highest relevance) |
| Traceability | URL only | evidence IDs, excerpt, source record |

The other real-run fixtures (time perception, nap, mirror, TikTok, "satt") state that their
research was *not* kept — itself a V1 traceability failure that V2's persisted package and
diagnostics fix. For a real OLD-vs-V2 comparison on saved projects use the audit script on the
Mac (3.5): it prints each project's stored package next to a fresh V2 run and never writes.

### 3.4 Real network test

Not possible in this container: the egress proxy denies Wikipedia, Brave and general web hosts
(`CONNECT tunnel failed, response 403`). `scripts/research_v2_audit.py` against the real
providers reported: discovery `wikipedia failed 403 Forbidden`, provider marked down, no further
calls, budget 2/6 searches, 0 documents, status `insufficient` → V2 degrades safely. The three
questions must be run on the Mac (3.5).

### 3.5 Real-Mac retest

```bash
cd ClipForge && git fetch origin && git checkout cloud/research-pipeline-v2
./.venv/bin/pip install -e "apps/api[dev]"          # adds scrapling (parser only)
cd apps/api
PYTHONPATH=. ../../.venv/bin/python scripts/research_v2_audit.py --compare-v1 \
  --question "Warum bleibt Essen in der Mikrowelle in der Mitte kalt?" \
  --question "Warum ist der Himmel auf dem Mars rot?" \
  --question "Warum wurde die Berliner Mauer gebaut?"
PYTHONPATH=. ../../.venv/bin/python scripts/research_v2_audit.py --projects 6   # OLD vs V2, read-only
```

Optional browser rendering for JS-only pages: `pip install "scrapling[fetchers]"`, `playwright
install chromium`, `RESEARCH_BROWSER_FETCH=true`.

Inspect per question: ROUTE (science / history / technology-or-everyday), SOURCES USED (types:
institutional/academic/government/reference before journalism/SEO), CORE ANSWER (a direct
answer, not a definition or advice), MECHANISM (`complete`, cause steps from retrieved text),
KEY EVIDENCE (sentences, no navigation/cookie text), CONFIDENCE and gaps, FAILURES/FALLBACKS
(robots/access/timeouts isolated), BUDGET (searches ≤ 6, documents ≤ 6, LLM ≤ 2). Then generate
the three videos in the app and check `state.research.package` / `diagnostics` of the new
projects: the hook and script claims should map to `core_answer` / mechanism facts, numbers
should exist in `package.evidence`, and readiness should not need the retry for these
questions.
