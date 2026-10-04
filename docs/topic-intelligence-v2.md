# Topic Intelligence V2 — relevance, discovery & auto topic selection

Branch: `cloud/topic-intelligence-v2` (base `test` @ `9ea2b7d`). V2 evolves the V1
package `apps/api/clipforge/topic_intelligence/` in place; it is not a second engine.

* Part 1 — Phase 0 audit of V1 (written before implementation).
* Part 2 — V2 design and what changed.
* Part 3 — Evaluation (offline harness, real run, Real-Mac retest).

---

## Part 1 — Phase 0: V1 audit

### 1.1 Trace of the current path

| Stage | Where (V1) | What it does |
|---|---|---|
| UI entry | `apps/web/src/app/page.tsx` (`TopicSuggestionChips`, textarea, Generate) + `apps/web/src/lib/topic-suggestions.ts` | Textarea = manual mode. Three chips + hidden reserve (6) loaded on Home mount; a chip only fills the textarea; "Neue Vorschläge" replaces chips. **No Full-Auto control in the UI** — `POST /next` exists but nothing calls it. |
| API | `topic_intelligence/routes.py` | `POST /next` (best unused candidate), `POST /candidates/{id}/skip`, `POST /suggestions` (chips), `GET /diagnostics` (dev only), `GET /status`. |
| Warm-up | `main.py` → `service.warm_pool_in_background` | One discovery at app start (single-flight `runtime.FLIGHT`). |
| Discovery providers | `sources.py` | `WikipediaPageviewsSource` (de.wikipedia **top-60 most-viewed articles of yesterday**, metadata for 40, 30-day history for 20); `YouTubeTrendingSource` (YouTube **most-popular chart**, DE, categories 27/28, 25 each, + channel baselines for 8 channels); `BraveNewsSource` (3 fixed news queries, last day). |
| Caching | `cache.py` | Normalized payload per provider/key in `topic_source_cache`, TTL 2–3 h (search probe 24 h), single-flight per key, `CallMeter` YouTube quota budget (400 units/refresh). Curator judgements cached 14 d (`semantic.py`). |
| Timeouts | `runtime.py` | Provider 30 s, curator 60 s (+5 s grace), AI deadline 150 s per flight, flight hard limit 300 s; `call_with_timeout` abandons hung threads. |
| Normalize / group | `service.group_topics`, `text.topic_key`, `text.similarity` | Sightings merged by order-independent token key or token similarity ≥ 0.72. |
| Prefilter | `service._discover` | Drops groups flagged person/tragedy/disambiguation and calendar pages (`date_page`). Everything else enters. |
| Curation order | `service._curation_priority`, `scoring.curation_priority` | Cheap evidence (trend, question-in-title, universal subject, article evidence, niche prior, corroboration, novelty) orders the raw pool for the bounded AI budget. |
| Candidate generation / question rewriting | `transform.py` + `semantic.py` | **One question per raw topic.** With an OpenAI key: `semantic-curator-v2` (one batched call per ≤10 topics) writes a grounded question from the headline/article and rates it (6 clarity dims + 5 short dims + 3 others + issue codes). Without a key: extract a question already in a title, or a template ("Warum bekommen wir X?", "Wie entsteht eigentlich X?") where the Wikipedia extract says what X is. |
| Deterministic scoring | `signals.py`, `scoring.py` (`ti-score-v6`) | 14 signals, weighted sum after confidence shrinkage to 0.5; obscurity / flag penalties; trend scaled by corroboration and short-worthiness. |
| LLM scoring | `semantic.curated_signal` → `semantic`, `suitability`, `broad_appeal`, `accessibility`, `visual`, `short_worthiness` signals | All six come from the same single curator answer. |
| Semantic gates | `scoring.rejection_reasons` / `semantic_rejections` / `short_rejections` | Any curator issue (except 3 soft ones), any clarity dim < 6/10, short-worthiness < 0.45, multi-part/list/survey shapes, prior-knowledge, quality floor 0.55. Without a judgement: strict local rules (why/how/what-if/paradox + fully accessible premise). |
| Dedupe | `service._discover` (`duplicate_in_pool`), `history.novelty_signal` (`duplicate_of_previous_topic`), `service._available_records` | Token-overlap coefficient ≥ 0.72 against kept questions / history. |
| Ranking | `scoring.rank` | Usable first, then `final_score`. |
| Final selection | `service._next_record` (first usable in rank order), `service._available_records` + `scoring.diversify` (reorders within 0.04 by niche/mechanism) | |
| YouTube integration | `sources.YouTubeTrendingSource`, `sources.YouTubeCompetitionProbe` (search.list 100 units, ≤2 probes per refresh), `signals.outlier_vs_channel` | Read scope of the connected channel; skipped when not connected. |
| Trend/current data | Wikipedia top list + 30-day history, YouTube chart rank, Brave outlets | `merge_trend` takes the strongest; 48 h half-life decay on `freshness_at` (the cache fetch time). |
| Own analytics | `history.own_performance_priors` | Niche-median views vs channel median; unavailable below `youtube_baseline_min_sample`. Weight 0.02. |
| Handoff to generation | `service.resolve_topic_provenance` / `mark_topic_used`, called from `main.py` `POST /api/generation-jobs` | A chip question is generated exactly like a typed one, plus server-side provenance. Manual input gets `topic_source=manual`. |

### 1.2 V1 history (what each round fixed and what it revealed)

| Commit | Real-Mac finding | Fix |
|---|---|---|
| `8ffa7b8` ti-score-v2 | Winners like "Was steckt eigentlich hinter 29. September?", a flight id | Quality weights, obscurity penalties, quality floor |
| `478888b` tq3 / v3 | 96 raw → 16 evaluated → **0 accepted** | Local question extraction, backfill, 12+ gate |
| `64a64d9` v4 | Only **2/9** passed a human review | Semantic gate |
| `d86d9b3` v5 | Only 4/60 reached validation | Combined curator |
| `6e347e5` v6 | Clear and broad but only **3/9** strong shorts | Short-worthiness |
| `9ae757c` | 95 raw → 47 evaluated → 27 curated → **0 accepted** | Smaller batches, prioritisation |

Every round tightened the gates on the same three trend feeds. Acceptance went down,
the survivors stayed "the least bad trending item, reframed as a question".

### 1.3 The ten questions — answered with code evidence

**1. Where do irrelevant candidates originate?** In discovery. All three providers are
"what is popular right now" feeds, none of which is a source of knowledge questions:

* `WikipediaPageviewsSource.fetch` takes the **top-60 most viewed** de.wikipedia articles
  (`TOP_LIMIT = 60`). That list is dominated by people, TV, sport, deaths and dates;
  `_wiki_flags` only marks them, the prefilter removes person/tragedy/disambiguation, and
  what remains are mostly entity names (countries, films, companies) with no inherent question.
* `YouTubeTrendingSource` reads the **most-popular chart** (raw views ranking) — video titles
  of big channels, not questions.
* `BraveNewsSource.QUERIES = ("Wissenschaft Forschung", "Gesundheit Studie", ...)` returns
  single-study news ("Studie zeigt ...") — the source of the survey/measurement questions
  that v6 had to reject.
* **There is no evergreen source at all.** A timeless high-curiosity question (Mars, sleep,
  lightning) can only appear when its article happens to spike. The cold-start pool therefore
  has nothing to fall back on.

**2. Discovery or ranking?** Primarily **discovery** (wrong supply: the pool contains very few
knowledge-short subjects), secondarily **ranking** (curiosity/payoff barely count, see 5).
Evidence: the Real-Mac funnels above (0/16, 2/9, 3/9, 0/47) — each stricter gate removed
more of the same pool instead of finding better candidates; the gates are mostly right,
the pool is wrong.

**3. Are generic LLM-brainstormed topics entering too early?** Not brainstorming — V1 never
asks an LLM for topics. The problem is the inverse: the curator is forced to **invent a
question for an entity/news item** (`CURATOR_INSTRUCTIONS` STEP 2: "write ONE natural
spoken German question ... You may reframe a headline"). The question is fused with the
topic (`Transformed` = one question per group), so a good topic with a weak first question
is lost, and a weak topic gets a "technically valid" question.

**4. Do weak candidates survive eligibility?** Yes. A curator answer of **6/10 on every
dimension** passes every gate: semantic dims ≥ 0.6, short-worthiness 0.6 > 0.45 floor,
mass-audience quality ≈ 0.65 > 0.55 floor. There is no gate on curiosity or payoff
themselves — only on the weighted mean (`short_worthiness`, floor 0.45). Without a key the
strict local rules accept any well-formed "Warum ...?" (`LOCAL_SHORT_BASE["why"] = 0.7`).

**5. Are the weights aligned with Knowledge Shorts?** No. Measured on `DEFAULT_WEIGHTS`:
`curiosity_strength` contributes **3.3 %** and `payoff_specificity` **2.6 %** of the score
(both only inside `short_worthiness`), while `accessibility` alone is 12 %. Clarity beats
curiosity 4:1 — exactly "technically valid but not worth watching". In addition **56 % of the
weight comes from one curator answer** counted five times (`semantic`, `suitability`,
`broad_appeal`, `short_worthiness`, `visual`, plus `accessibility` via `min`): effectively one
opaque LLM score.

**6. Is "interesting now" backed by current evidence?** Partly. Trend values are real
(Wikipedia ratio, chart rank, outlet count), but: (a) the evidence is attributed to a
*reframed* question that may only loosely relate to the trending item; (b) a signal has no
own `fetched_at`/TTL — freshness is `group.newest` (cache fetch time) with a 48 h half-life,
so stale evidence fades but never loses "trend" status; (c) `wikipedia_top_rank_only`
(no history) turns raw top-list rank into "trend". No label such as "Trending" is shown, so
nothing false is displayed — but nothing true is either.

**7. Are we overvaluing generic popularity?** In discovery, yes: the Wikipedia *top* list and
the YouTube *most-popular* chart are raw-popularity rankings; `trending_chart_trend` gives
any chart video 0.55–0.90 "medium" trend from its rank alone. The relative outlier
(`outlier_vs_channel`, robust median/MAD) is good but only applied to chart videos, which are
already giants.

**8. Are questions too broad / trivial?** Templates produce broad questions
("Wie entsteht eigentlich ein Regenbogen?"); news reframing produces narrow study-of-the-day
questions. There is no check for a trivial answer ("Ist Wasser nass?"), a speculative premise
or a myth.

**9. Is semantic dedupe sufficient?** No — `text.similarity` is an overlap coefficient over
content tokens, so it **over-collapses** short questions and **misses** paraphrases
(measured): "Warum schlafen wir?" vs "Warum träumen wir im Schlaf?" = 1.0 (distinct
questions, collapsed); "Warum gähnen wir?" vs "Ist Gähnen ansteckend?" = 1.0 (collapsed);
"Warum ist der Mars rot?" vs "Woher hat der Rote Planet seine Farbe?" = 0.0 (same question,
missed: "rote" is not stemmed to "rot", no "Roter Planet" = Mars). The required pair works
(Mars rot vs Mars Staubstürme = 0.5, distinct).

**10. Are the three suggestions too similar?** They can be: `scoring.diversify` only reorders
inside a 0.04 score window by keyword niche and mechanism; there is no subject/topic-family
notion, so three questions about the same subject from different sources are served side by
side whenever they are not ≥ 0.72 similar.

Also found: Full Auto (`next_topic`) picks the first usable candidate with no minimum quality
beyond the gates and is not reachable from the UI; a used topic excludes its whole group key
forever (`_recent_status_keys`), which would block an entire evergreen subject after one video.

### 1.4 Classification of V1 components

| Component | Verdict | Why |
|---|---|---|
| `runtime.py` (single-flight, timeouts, stages) | **KEEP** | Bounded, observable, proven on the Mac. |
| `cache.py` (TTL cache, `CallMeter`) | **KEEP** | Correct quota protection. |
| `sources.WikipediaPageviewsSource` | **IMPROVE** | Keep as momentum source; also feeds demand level. |
| `sources.YouTubeTrendingSource` | **IMPROVE** | Chart rank is popularity, not momentum; keep the per-channel outlier. |
| `sources.BraveNewsSource` | **KEEP** (lower trust) | Timely evidence only; never evergreen. |
| `sources.YouTubeCompetitionProbe` + `competition_estimate` | **IMPROVE** | Supply ≠ bad: becomes demand + opportunity. |
| `signals.outlier_vs_channel` | **KEEP** | Robust, context-normalized, sample-size confidence. |
| `signals.wikipedia_trend` | **KEEP** (+ freshness metadata) | |
| Evergreen discovery | **ADD** | Missing signal class (root cause 1). |
| `semantic.py` curator v2 | **IMPROVE** → v3 | Question formation from candidate questions, topic family, truthfulness issues. |
| `transform.py` | **IMPROVE** | Evergreen seed selection; topic ≠ question. |
| `scoring.py` ti-score-v6 weights | **REPLACE** → ti-score-v7 | Explicit curiosity/payoff/knowledge dimensions, evidence classes, confidence-aware rank. |
| `scoring` gates (12+, grounding, shapes, semantic) | **KEEP** + extend | They work; add curiosity/payoff/trivial/myth/stale-event gates. |
| Obscurity penalties, trend-quality factor | **KEEP** | Still useful for live entity topics. |
| `wikipedia_top_rank_only` as trend | **REMOVE** | Raw rank is not momentum. |
| `text.similarity` for dedupe | **REPLACE** (for dedupe only) | Over-collapse + paraphrase misses. Kept for grouping/curation features. |
| `scoring.diversify` | **IMPROVE** | Topic family + semantic relatedness. |
| `history.novelty_signal` | **KEEP** (better equivalence inside) | |
| `history.own_performance_priors` | **KEEP** behind an explicit evidence hook | Future analytics. |
| `service` orchestration, records, routes, handoff | **KEEP** | Add Full Auto, labels, diagnostics. |
| Whole-group exclusion of used topics forever | **IMPROVE** | Cool-down instead of a permanent subject block. |
| Home chips UI | **IMPROVE** | Reasoning + signal label; add "Generate automatically". |

---

## Part 2 — V2 design (what changed)

Score version **`ti-score-v7`**, curator **`semantic-curator-v3`**, question step **`tq4`**,
catalog **`evergreen-catalog-v1`**. Pools, curator cache entries and Home chips of older
versions are not reused (Home drops stored chips once: storage version 5).

### 2.1 Flow

Old (V1):

```
3 trend feeds -> group -> prefilter -> curation priority -> curator writes ONE question per topic
  -> gates -> weighted sum (clarity-heavy, missing = neutral) -> rank -> 0.04-window niche diversity
  -> chips | /next (first usable)
```

New (V2):

```
DISCOVER            Wikipedia top list, YouTube DE chart, Brave News  +  editorial evergreen subjects
                    (evergreen.py: 69 subjects, 24 per refresh, demand + momentum from real pageviews)
NORMALIZE           sightings of one subject merge (a trending "Polarlicht" joins the evergreen subject)
HARD ELIGIBILITY    prefilter (person / tragedy / calendar page), used-topic cool-down (21 d)
DEDUPE              never the same question again (candidate id + novelty via question equivalence)
CHEAP EVIDENCE      curation priority = momentum, demand LEVEL, channel-relative outliers (never raw views)
SHORTLIST           bounded AI budget; every curator batch mixes evergreen and live topics (<= 60 % each)
QUESTION FORMATION  topic != question: curator picks/improves among candidate questions (seeds) or writes one;
                    without a key: strongest unused seed / extracted question / concrete template
SEMANTIC EVALUATION V1 gates (clarity, 12+, grounding, shapes) + V2 gates (curiosity, payoff, trivial,
                    speculative, myth premise, stale current event)
CONFIDENCE-AWARE    ti-score-v7 weighted sum - penalties - explicit confidence cost
RANKING             semantic dedupe (question equivalence or curator subject+aspect) with duplicate_of
SELECT              3 suggestions (family-aware diversity) | Full Auto (own minimum, widen once, or none)
```

### 2.2 Discovery sources and the evergreen class

| Source | Evidence it may create | Budget | Failure |
|---|---|---|---|
| `wikipedia_pageviews` (top list) | momentum (recent vs own median), demand level (median views/day) | 1–2 + 2 + ≤ 20 requests, 3 h cache | failed → others continue |
| `youtube_trending_de` (chart) | momentum 0.4–0.7 (rank), channel-relative outlier | ≤ 400 quota units / refresh | skipped when not connected |
| `brave_news_de` | timely momentum (outlets, last day) | 3 requests, 2 h cache | skipped without key |
| `editorial_evergreen` (**new**) | **none by itself**; demand + momentum only from the subject's real pageview history | 24 requests (one per subject), 1 day cache, stops after 3 consecutive failures | subjects still offered, honestly without evidence |
| `youtube_search_competition` (probe) | opportunity + related-Shorts demand (median views/day) | ≤ `TOPIC_YOUTUBE_SEARCH_PROBES` (2) × 100 units | lowers confidence only |

The catalog lists subjects and seed questions, never scores; every seed passes the same
gates and the curator may reject or rewrite it. It is the supply that makes cold start work,
not a list of winners.

### 2.3 YouTube signals and outlier logic

* Raw views are **never** a score input. The chart gives moderate momentum from rank only.
* Outlier (kept from V1): a chart video's views/day vs the **median views/day of the channel's
  recent uploads** (robust median/MAD, sample-size confidence: < 3 uploads = unavailable,
  ≥ 10 = high). Evidence persisted: ratio, robust z, sample size, channel median.
  Example: 50 k views at 10× its channel's level beats 5 M views on a normal day (harness).
* Search probe (shortlist only): related recent Shorts → supply (`competition`), demand
  (median of related videos' views/day; < 3 related = no evidence) and opportunity.
* Missing data lowers confidence; nothing is extrapolated.

### 2.4 Scoring dimensions and weights (`scoring.DEFAULT_WEIGHTS`)

| Group | Dimension (signal) | Weight | Source |
|---|---|---|---|
| Worth watching 0.44 | curiosity (`curiosity`) | 0.15 | curator curiosity_strength/gap; local: question mechanism (low) |
| | payoff (`payoff`) | 0.13 | curator payoff_specificity, clear_factual_payoff, reveal |
| | short-form fit (`short_worthiness`) | 0.09 | V1 signal (focus, concreteness, weak shapes) |
| | knowledge value (`knowledge_value`) | 0.07 | curator (or knowledge_short_fit + concreteness) |
| Evidence 0.22 (bonus-only) | demand (`demand`) | 0.08 | Wikipedia median views/day, related Shorts |
| | momentum (`trend`) | 0.06 | Wikipedia ratio, chart rank, news outlets — fresh only |
| | outlier potential (`outlier`) | 0.05 | channel-relative |
| | competition / opportunity (`opportunity`) | 0.03 | supply read against demand |
| Fit 0.30 | novelty 0.06, accessibility 0.06, broad appeal 0.05, visual 0.05, channel fit 0.04, researchability 0.04 | | |
| Priors 0.04 | question form 0.02, own performance 0.02 | | |
| Measured, weight 0 | `semantic`, `suitability`, `competition` | 0 | gates / diagnostics only |

* **Bonus-only evidence**: missing or stale demand/momentum/outlier counts **0** — not neutral,
  not positive. Quality signals keep V1's neutral prior when not assessed.
* **Confidence** (`overall_confidence`): weight-coverage of signal confidences, excluding optional
  analytics (their absence costs a cold-start candidate nothing). The final score subtracts
  0 / 0.02 / 0.05 for high / medium / low — visible as `confidence_penalty` and
  `score_before_confidence`.
* Weights stay overridable via `TOPIC_SCORE_WEIGHTS` (versioned `ti-score-v7+w…`).

### 2.5 Hard eligibility (removed, not ranked lower)

V1 gates unchanged (thresholds identical): duplicate of a previous topic, reject flags,
channel fit, prior knowledge (12+), semantic clarity dims ≥ 6/10 and issue codes, multi-part /
list / survey shapes, short-worthiness floor, quality floor; strict local rules without a curator.
V2 adds:

| Reason | Rule |
|---|---|
| `low_curiosity` | curator curiosity_strength < 7/10 ("technically valid but not worth watching") |
| `weak_payoff` | curator payoff_specificity < 6/10 (curiosity without payoff = clickbait) |
| `semantic_trivial_answer` | curator: obvious / one boring fact |
| `semantic_speculation_dependent` | no settled answer exists |
| `semantic_misleading_premise` | myth or false premise (a myth may only be asked as "Stimmt es, dass …?") |
| `stale_current_event` | news-only or `current_event_only` question without **fresh** momentum |

None of these is a keyword list; the deterministic shape checks of V1 remain as a backstop.

### 2.6 Question formation (topic vs question)

`RawTopic`/`TopicGroup` = subject; the candidate question is formed separately:
curator v3 receives `candidate_questions` (unused seeds) and may choose, improve or replace them;
it must fit the form to the mechanism (why / how / what if / "Stimmt es, dass" / "Woher wissen
wir" …), not force "Warum". Without a key the strongest **unused** seed is chosen (paradox >
what-if > why > how; weak shapes never). Unchosen seeds are kept as `provenance.alternatives`.
A used question never returns; its subject may return with another question after 21 days.

### 2.7 Semantic dedupe and diversity

* `text.question_equivalence`: soft **Dice** over concept tokens (both questions must share their
  concepts), with grammar-only verbs dropped, possessives ignored, colour words → "Farbe",
  "Roter Planet" → Mars. Mars rot ≡ "Was macht den Mars eigentlich rot?" (1.0) ≡ "Woher hat der
  Rote Planet seine Farbe?" (0.8); vs "… Staubstürme?" 0.33 (distinct); "Warum gähnen wir?" vs
  "Ist Gähnen ansteckend?" 0.67 (distinct). Used for pool dedupe, chip dedupe and novelty.
* LLM-assisted: curator `subject` + `aspect`; same subject and equivalent aspect = same video.
* Every collapse records `duplicate_of` (id, question, equivalence).
* Diversity (`scoring.diversify`): within 0.04 prefer another niche/mechanism; a candidate that
  repeats an already chosen **subject** may be passed by a different subject up to 0.08 weaker,
  never by a dramatically weaker one. One subject never blocks a domain.

### 2.8 Trend freshness and classes

Every time-sensitive signal carries `fetched_at` + `ttl_hours` (Wikipedia 72 h, chart 24 h,
news 36 h, demand level 30 d). `scoring.classify_signal(candidate, now)` — re-run at serve time:

| Class | Requires (all fresh) | UI label |
|---|---|---|
| `TRENDING` | momentum ≥ 0.6, confidence ≥ medium, corroborated by 2 sources or demand ≥ 0.5 | Trend |
| `TIMELY` | fresh news momentum from ≥ 2 outlets | Aktuell |
| `EMERGING` | momentum ≥ 0.35 | Im Kommen |
| `EVERGREEN_WITH_CURRENT_INTEREST` | evergreen evidence + any of the above | Zeitlos · gerade gefragt |
| `EVERGREEN` | editorial evergreen subject or stable demand (≥ 0.35, ≥ medium), not news-only | Zeitlos |
| none | anything else | no label |

Stale momentum loses its class immediately (also inside a reused pool) and contributes 0.
If live signals fail, evergreen candidates remain — with low confidence and no trend claim.

### 2.9 Competition / opportunity

`signals.opportunity(competition, demand)`: ≥ 2 near-identical (or ≥ 3 strong + 1 identical)
Shorts = `saturated_generic` (0.3); demand exists (≥ 3 related or demand ≥ 0.5) and no identical
Short = `specific_opportunity` (0.8); no related videos and no demand = `low_demand` (0.4);
else `open` (0.6). Always flagged `estimate`; confidence medium only with ≥ 10 results.

### 2.10 Cost / request budget (per pool; `GET /status` → `budgets`)

| Budget | Value |
|---|---|
| Wikipedia | 1–2 top list + 2 metadata + ≤ 20 histories |
| Evergreen pageviews | 24 (cached 1 day) |
| Brave | 3 |
| YouTube | ≤ 400 quota units, ≤ 2 search probes |
| Candidates evaluated | 60 (target 9 accepted) |
| AI requests | 3 × ≤ 10 topics; widening once: +1 request, ≥ 20 evaluations |
| Timeouts | provider 30 s, curator 60 s, AI deadline 150 s, flight 300 s |

Cheap filtering and dedupe happen before any AI call; caches are reused across refreshes.

### 2.11 Modes

* **Manual** — the textarea + Generate, unchanged; `topic_source=manual`, never passes through
  Topic Intelligence.
* **Assisted** — the three chips ("Neue Vorschläge" = "Give me 3 ideas"), each with a German
  "Warum es funktionieren könnte" reason and an evidence-backed label; scores stay in diagnostics.
* **Full Auto** — new `POST /api/topic-intelligence/auto` + "Generate automatically" button:
  first usable candidate meeting `scoring.auto_eligibility` (medium/high confidence: score ≥ 0.55,
  curiosity ≥ 0.65, payoff ≥ 0.65; low confidence: only an editorial evergreen question with
  score ≥ 0.45). Otherwise widen once, then `no_strong_candidate` — nothing starts. A selected
  question is started through the normal `POST /api/generation-jobs` with provenance.

### 2.12 Failure isolation

| Failure | Behaviour |
|---|---|
| YouTube unavailable | chart/probe skipped; evergreen + Wikipedia + news continue |
| Trend source unavailable | no momentum → no Trend/Aktuell/Im Kommen label |
| Wikimedia unreachable | evergreen subjects offered without evidence (low confidence, label "Zeitlos" from the catalog only) |
| LLM unavailable | strict local rules; Full Auto only via the evergreen fallback |
| Analytics provider fails | skipped and recorded; cold-start signals kept |

### 2.13 Future analytics injection point

`evidence.py`: `CandidateEvidenceProvider.signals(candidate) -> {name: Signal}` passed via
`DiscoveryDeps.evidence_providers` (empty in cold start). Allow-listed names only
(`ANALYTICS_SIGNALS = {"own_performance"}`): a provider can never create demand, momentum,
outliers or trend labels; failures are isolated; its weight lives in the scoring authority and is
confidence-scaled; its absence never lowers confidence (`scoring.OPTIONAL_SIGNALS`). Retention,
"stayed to watch", topic-family and mechanism performance plug in here without touching discovery.

### 2.14 Diagnostics ("why did this question win?")

`GET /api/topic-intelligence/diagnostics` (dev) and `scripts/topic_diagnostics.py` show per
candidate: topic family, origin, final question, alternatives, sources, evidence freshness
(`fetched_at`/TTL), eligibility and rejection reasons, all component scores, confidence and its
penalty, signal class and basis, `duplicate_of`, final rank, `selection_reason`, Full-Auto
eligibility. `--evaluate` runs the real path and prints the report below.

---

## Part 3 — Evaluation

### 3.1 Offline harness (`tests/test_topic_intelligence_v2_eval.py`, 22 tests)

One fixture through the real pipeline (scripted curator, time-stamped evidence) with: excellent
evergreen question, strong current question with fresh corroborated evidence, stale fake trend,
huge raw-view generic chart video, relative outlier, duplicate from an unrelated headline, broad
weak topic, trivial payoff, low visual potential, speculative/low researchability, saturated
generic angle, specific angle, missing evidence, high curiosity/poor payoff, payoff/low
curiosity. Proven: strong candidates outrank weak ones; stale evidence cannot stay trending (also
at serve time); raw views do not dominate; duplicates collapse with `duplicate_of`; the three
suggestions are distinct subjects; cold start works without analytics; no unsupported label;
a weak-only pool yields nothing (Full Auto widens once, then `no_strong_candidate`); Full Auto
respects its minimum; manual mode is untouched. Regression fixtures for the V1 audit findings:
6/10-everywhere question, curiosity/payoff weights, top-list rank as trend, dedupe
over-collapse/paraphrase miss, permanent subject block, stale news question.

Component tests (`test_topic_intelligence_v2_components.py`, 14): evergreen source bounds,
caching, no-network behaviour and widening window; evidence-provider allow-list and failure
isolation; source mix; family diversity; TTL staleness; demand level vs spike; opportunity states.

### 3.2 Real run in the cloud container (2026-10-04)

`scripts/topic_diagnostics.py --evaluate` against a fresh database. **Networking is blocked**
in this environment (the egress proxy denies `wikimedia.org`, `de.wikipedia.org`; 403), and
there is no OpenAI key, no Brave key and no connected YouTube channel. So this run proves
failure isolation and the cold-start fallback — not live relevance:

| Source | Result |
|---|---|
| wikipedia_pageviews | failed (proxy) — others continued |
| youtube_trending_de / probe | skipped (not connected) |
| brave_news_de | skipped (no key) |
| editorial_evergreen | partial: 24 subjects offered, stopped after 3 failed pageview requests, **no evidence invented** |
| curator | unavailable → strict local rules |

Top 3 (assisted): "Warum brennt die Sonne, obwohl es im All keinen Sauerstoff gibt?",
"Warum wird Brot an der Luft hart, Kekse aber weich?", "Wie kann ein Kopfhörer Lärm mit Schall
auslöschen?" — all `EVERGREEN` ("Zeitlos"), confidence **low**, no live evidence.
Full Auto: the Sonne question via the evergreen fallback. In the real app (Home, screenshot
verified) "Generate automatically" queued exactly that question with Topic-Intelligence provenance.

Critical reading: these are clearly better Knowledge-Short questions than V1's documented Mac
output ("Was steckt eigentlich hinter 29. September?", "Woran erkennt man ein gutes Passwort?",
"Wie stark ist das Gerechtigkeitsempfinden in der Bevölkerung?"): one concrete mechanism each,
a curiosity gap, three different subjects. But they come from the editorial catalog, ranked only
by question form (all within 0.04) — they demonstrate the floor V2 guarantees offline, not the
evidence-driven ranking. Whether live discovery (Wikipedia/YouTube/news + curator) now beats
the evergreen floor, and whether the 7/10 curiosity gate leaves enough candidates, must be
judged on the Mac with network and keys.

### 3.3 Real-Mac retest

1. `git fetch origin && git checkout cloud/topic-intelligence-v2` (do not merge), then
   `npm run dev` (API + web). Keep `OPENAI_API_KEY` set; YouTube connected with read access;
   Brave key optional.
2. Live evaluation (runs the real path, never starts a generation):
   `cd apps/api && PYTHONPATH=. ../../.venv/bin/python scripts/topic_diagnostics.py --evaluate`
   Inspect: every source `ok/cached/partial`; `editorial_evergreen` says `measured=24`;
   the 3 suggestions and the auto winner, each with CATEGORY, WHY, EVIDENCE (freshness), CONFIDENCE.
3. `… topic_diagnostics.py --status`: `budgets`; curator requests ≤ 3 (+1 widening) and their
   seconds; `rejection_reasons` — note how often `low_curiosity` / `weak_payoff` fire and
   whether ≥ 3 candidates were accepted. If the 7/10 curiosity gate starves the pool, report the
   counts (do not lower it blindly).
4. `… topic_diagnostics.py --limit 15`: per candidate `v2:` rank, class, family, origin
   (evergreen vs live), auto eligibility, `duplicate_of`, selection reason.
5. Home: three chips with a label (Trend / Aktuell / Im Kommen / Zeitlos / Zeitlos · gerade
   gefragt) and "Warum es funktionieren könnte". Check: a "Trend"/"Aktuell" label only where a
   real current spike/news exists; three different subjects; no paraphrase pairs.
   "Neue Vorschläge" replaces them.
6. "Generate automatically": a note "Automatisch gewählt: „…“" and a new queue entry — or the
   message that no question is strong enough (then nothing must start). Open the project and
   check its provenance (`topic_source: topic_intelligence`).
7. Manual: type your own question → Generate → unchanged behaviour (`topic_source: manual`).
8. Judge honestly: are the three suggestions ones you would actually make, and better than the
   last V1 suggestions? Which were live vs evergreen?

---

## Part 4 — Real-Mac calibration: selection bias, outliers, why-now, partial sources

Real Mac (`c2f61b0`): top 3 "Warum funktioniert ein QR-Code auch dann noch, wenn er zerkratzt
ist?", "Warum bleibt Essen in der Mikrowelle in der Mitte kalt?", "Warum brennt Chili im Mund,
obwohl es gar nicht heiß ist?" — good — but `raw_groups=71 budget=60 evaluated=20 target=9
accepted=9 remaining=51`, and QR / Mikrowelle both showed `outlier = 1.0` with the reason
"ein Video dazu ist gerade in den deutschen YouTube-Charts".

### 4.1 Early-stop root cause

`service._curate_pool` ran `while fresh and evaluated < budget and target_left() > 0` with
`target_left = TARGET_ACCEPTED - accepted`. With 10 topics per curator request, two requests
produced 9 accepted candidates and the loop ended — with one AI request and 40 evaluations of
budget left. The local loop had the same condition (`accepted < TARGET_ACCEPTED`).

The first 20 were ordered by the cheap curation priority (demand, question already in the title,
universal subject, grounding, niche prior, corroboration, novelty, minus penalties), with the
evergreen/live share capped at 6 of 10 per batch. Biases: no per-source representation (a
source whose titles are statements started at `question_strength` 0.45 vs 0.7 for evergreen
"why" seeds); no subject/niche spread (several food topics in a row could fill a batch); and
the target stop meant the 51 later topics were never compared at all. **Yes, a materially
stronger candidate could be skipped** — the harness reproduces it (a strong topic with a
slightly lower pre-score at position 25 was never evaluated).

### 4.2 New evaluation policy (bounded)

```
all normalized topics -> cheap deterministic pre-score (no raw views)
  -> representative shortlist (representative_order):
       reserve each source's 2 best viable topics (pre-score >= 0.35, no hard penalty),
       order by pre-score - 0.03 x same-niche repeats (max 3), evergreen/live <= 60 % each,
       drop topics whose cheap question repeats an earlier one (cheap_duplicate, never evaluated)
  -> expensive evaluation in that order, until ALL of:
       accepted >= 9                              (enough supply)
       evaluated >= 20                            (minimum coverage)
       every shortlisted source has its seeds evaluated
       no remaining topic within 0.05 of the cheap pre-score of the current top 3
     or a budget ends it (60 evaluations, 3 AI requests, AI deadline)
```

The evaluation report records `shortlist` (size, per-source, evergreen/live, deferred
duplicates), `evaluated_sources`, `stopped_by` and the `stop_rule` (bar, competitive topics).

**Cost:** the AI budget is unchanged (≤ 3 requests × 10 topics per pool, +1 once when widening).
A pool that used 2 requests on the Mac now typically uses the 3rd while competitive topics
remain — about one more request (~10 topics, one curator call's latency) per pool refresh,
in the background warm-up. Local mode (no key) evaluates the whole bounded pool (≤ 60 topics,
deterministic, negligible cost).

### 4.3 Outlier = 1.0 — the two paths and the calibration

The persisted records of the Mac run are not in this repository, so the exact numbers must be
read there: `scripts/topic_diagnostics.py --trace "QR-Code"` and `--trace "Mikrowelle"` print
SOURCE VIDEO, CHANNEL, VIDEO AGE, VIEWS, VIEWS/DAY, CHANNEL BASELINE, SAMPLE SIZE, RAW RATIO,
NORMALIZED SCORE, CONFIDENCE and the caps for every matching candidate (records persisted by
`c2f61b0` lack the video identity; run `--evaluate` first). What the code shows:

1. **Chart path (matches the reason text)** — the reason "ein Video dazu ist gerade in den
   deutschen YouTube-Charts" is only produced when the candidate's group contains a YouTube
   chart sighting. Neither evergreen subject is a chart video, so a chart video was merged into
   the group by `group_topics`, whose overlap coefficient gives **1.0 for one shared token**:
   "QR-Code" has the single content token `code` ("QR" is shorter than 3 letters), so any chart
   video with "Code" in its title merged; "Mikrowelle" likewise merges any title containing
   "Mikrowelle". That video's views/day vs its own channel's recent median (≥ 10× → 1.0) was then
   credited to the QR / Mikrowelle question. The ratio may be legitimate **for that video**; its
   attribution to the question was not.
2. **Probe path** — for the top-2 probed candidates, `competition_estimate` filled an empty
   `outlier` with the BEST related search result's views vs its channel's LIFETIME mean
   (`log_scale(best, 10)`, ≥ 10× → 1.0, "low" confidence): a maximum over up to 25 videos, not
   age-normalized, and available only to the already top-ranked candidates.

Changes:

| | Before | After |
|---|---|---|
| Grouping | overlap coefficient ≥ 0.72 (one shared token merges) | same key, or question equivalence ≥ 0.72 of the titles (qualifiers in brackets ignored) |
| Probe "outlier" | scored (max ratio vs lifetime mean) | diagnostics only (`competition.evidence.related_outlier`), never scored |
| Channel baseline | `max(1, median)` | median < 20 views/day → unavailable (`channel_baseline_too_small`) |
| Tiny samples | n ≥ 3 could reach 1.0 | n < 5 capped at 0.6, n < 8 at 0.85; 1.0 needs ≥ 8 uploads |
| Channel spread | ignored | robust z < 2 (when spread is known) → capped at 0.5 |
| Traceability | ratio, sample, median | + source video title, channel, id, views, age, views/day, raw curve, caps |

A legitimate strong outlier (≥ 10× a stable ≥ 20 views/day baseline over ≥ 8 uploads) still
scores 1.0 (test).

### 4.4 Why-now wording

Chart wording is now literal: "ein Video mit genau dieser Frage ist gerade in den deutschen
YouTube-Charts" only when a chart title is equivalent to the question; otherwise "das Video
„<Titel>“ zum selben Thema ist gerade in den deutschen YouTube-Charts" (same subject is guaranteed
by the stricter grouping); no chart sighting → no chart claim. Wikipedia: "die Wikipedia-Aufrufe
zum Thema liegen gerade beim N-fachen des Normalniveaus"; outliers name their video.

### 4.5 Partial sources

* `youtube_trending_de partial — chart Bildung (27) in DE: not_found`: harmless. YouTube offers
  no most-popular chart for category 27 in DE; the Wissenschaft & Technik chart (28) still
  delivers. There is no general-chart fallback (only 27/28 are ever requested), the missing
  category creates no topics and no trend, and the status says `partial` with the reason.
* `editorial_evergreen partial — HTTP 429`: previously each subject was still requested (429s
  that were not consecutive never tripped the 3-failure stop). Now `default_http_get` raises
  `RateLimited` on 429; the evergreen fetch and the Wikipedia history requests **stop at the
  first 429** in that refresh, keep what was measured (the rest is offered without evidence),
  and a rate-limited payload is cached for at most 1 hour (`cache.RATE_LIMITED_TTL`) instead of
  a day — no retry storm, no lost evidence for a whole day.
* A failing source is recorded as `failed`/`partial`; the others continue (test).

### 4.6 Tests (`tests/test_topic_intelligence_v2_fair_evaluation.py`, 18)

Fair coverage before stopping; a high-potential late candidate reaches the final top; stop once
nothing competitive remains (no extra request); pre-score ignores raw views; source
representation without first-batch displacement; evergreen/live bound; duplicate clusters skip
the semantic budget; tiny samples, near-zero baselines and in-spread videos cannot reach 1.0; a
legitimate outlier still does; lexically related chart videos no longer join a subject; chart
wording matches the evidence; 429 stops and short cache; missing chart category; failing source
isolation.

### 4.7 Real-Mac recheck

```
cd apps/api
PYTHONPATH=. ../../.venv/bin/python scripts/topic_diagnostics.py --evaluate
PYTHONPATH=. ../../.venv/bin/python scripts/topic_diagnostics.py --trace "QR-Code"
PYTHONPATH=. ../../.venv/bin/python scripts/topic_diagnostics.py --trace "Mikrowelle"
```

Compare the new top 3 with the old one; check `stopped_by`, `evaluated_sources`,
`shortlist.deferred_duplicates`, and that outliers now name a video on the same subject.
