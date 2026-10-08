# Visual Sources & Retrieval V2

Branch `cloud/visual-sources-retrieval-v2`. Integrates into the existing Visual
Director V2 / routed open-media pipeline; no working authority was replaced.
Rights policy, deterministic metadata relevance, OpenCLIP thresholds, payoff
protection, budgets, Visual Director fallback chain and the Final Video Critic
are unchanged and remain authoritative. V2 adds planning, routing, judging,
transformation planning and a final gate around them.

## Architecture

Before:

```
visual_intent ─► build_visual_query_plan (≤3 authored queries, else narration words)
             ─► route_sources (keyword class: space/historical/anatomy/entity/general)
             ─► provider search (same query string for every provider)
             ─► rights ─► media_relevance ─► OpenCLIP shortlist ─► real_media_quality_gate
             ─► sort (tier, coverage, CLIP, metadata) ─► tiny novelty tie-break
             ─► first accepted ─► Visual Director fallback (AI image / reuse / graphic)
render: complete_project_visuals (missing scenes only) ─► smart crop + alternating Ken Burns
```

After (new pieces in **bold**):

```
scene + sibling intents + script block + Story Arc facts + topic
  ─► **Search Planner** (visual_search_planner.py): signals, domain, sensitivity,
       faceted queries, per-source-family phrasings; same payoff protection
  ─► **Source Router V2** (source_router.py): domain order, unsuitable providers
       skipped and recorded, archive/space/commons phrasing per stage
  ─► provider search (incl. **optional strict Flickr**) ─► rights ─► relevance ─► OpenCLIP
  ─► existing quality gate ─► novelty tie-break
  ─► **Visual Judge** (visual_judge.py): pre-filter, 8 structured scores, hard
       semantic/factual floors, optional bounded VLM veto, judge-ordered ranking
  ─► scene gate (existing gate + **judge verdict** for fresh acquisition)
  ─► **AI fallback rules** (visual_fallback_policy.py)
  ─► **Transformation plan** (visual_transform.py) persisted on the scene
render: complete_project_visuals ─► **final visual quality gate**
        (visual_quality_gate.py) ─► permitted fallback or kept + reported
renderer: subject-aware crop + **planned Ken Burns move** (speed stays with pacing)
```

## Sources

| Source | Status | Notes |
| --- | --- | --- |
| Pexels, Pixabay | unchanged | provider-wide terms; video + photo |
| Wikimedia Commons | unchanged | asset-level `extmetadata` rights |
| Openverse, NASA, Europeana, LOC | unchanged adapters, new routing/phrasing | see `visual-sources-v2.md` |
| **Flickr (optional)** | new, `FLICKR_API_KEY` | licenses requested **and re-checked per row**: CC BY 2.0 (4), CC0 (9), PDM (10), CC BY 4.0 (11). NC/ND/SA, "no known restrictions" (7) and US-Gov (8) are never accepted. Bad key disables the provider; key never in errors, repr or cache keys. |

No paid stock provider was added.

## Search Planner

For each scene the planner reads the scene's planned intent (or, for a
narration-fallback fragment, the planned intent of a sibling scene of the same
fact), the full block text and linked facts, and the project topic. Signals:
entities, location, period (year/decade), alternate terms, domain and factual
sensitivity. The script planner now returns these as optional English fields
on each `visual_intents` entry (`entities`, `location`, `time_period`,
`alternate_terms`, `factual_sensitivity`); deterministic fallbacks apply when
they are absent (capitalised names in English planner fields, years from the
story context).

Facets: `subject_action`, `entity_period`, `entity_location`, `narrow`
(entity + subject), `entity`, `alternate`, `location_subject`,
`period_subject`, `broader`. Rules:

- Authored queries stay first; only *factual* facets (names, dates, places,
  alternate terms) are added beside them. Non-factual facets are recorded
  with `retrieval: false`.
- Narration-fallback scenes use the planned facets instead of narration words
  (`query_quality: planned_facets`).
- Meta wording ("show the real-world mechanism") and near-duplicates (token
  Jaccard ≥ 0.8) are dropped.
- Payoff protection: a facet is blocked by protected terms, any word of the
  protected payoff text (morphological match), and the distinguishing terms of
  any authored query the existing protection blocked (so "swedish" is blocked
  when the planner's own "swedish archipelago" query was protected against
  "Schweden").
- The existing 3-logical-query cap and 18-request budget are unchanged.

`visual_query_plan.search_plan` persists domain, sensitivity, signals,
facets, protected facets and `source_queries` per family.

## Source Router

| Domain | Order (absent = skipped) |
| --- | --- |
| space (incl. Mars/Moon/planets, DE+EN) | nasa, wikimedia, openverse, flickr, pexels, pixabay |
| historical | loc, europeana, wikimedia, openverse, flickr, pexels, pixabay |
| art/culture/artifact | europeana, wikimedia, openverse, loc, flickr, pexels, pixabay |
| anatomy | wikimedia, openverse, pexels, pixabay |
| diagram/illustration | wikimedia, openverse, pexels, pixabay |
| named entity/place | wikimedia, openverse, flickr, pexels, pixabay |
| nature/species | pexels, wikimedia, pixabay, openverse, flickr |
| everyday/general | pexels, pixabay, openverse, wikimedia, flickr |

Archives, NASA and Commons-style engines receive the planner's catalogue
phrasing for stage *i* (e.g. `berlin wall 1961`); stock libraries keep the
visual logical query. `media_search.routing` records domain, order and every
skipped provider with `unsuitable_for_domain:<domain>`; each provider stat
records `query_sent`.

## Visual Judge

Deterministic pre-filter: unusable/unknown license, unsupported type, missing
media, short side < 480, aspect wider than 2.6:1 (no meaningful 9:16 crop),
effective long side after 9:16 reframe < 600 px (> 3.2× upscale),
watermark/© markers, duplicate asset keys.

Scores in [0, 1] (weights): semantic_match .30, factual_match .25,
visual_impact .12, vertical_fit .10, quality .08, novelty .08, continuity .04,
license_confidence .03.

- **semantic_match** = existing metadata confidence/tier blended 50/50 with
  the existing OpenCLIP scene score (0.20→0, 0.34→1); unverified = metadata × 0.9.
- **factual_match**: period/setting contradictions → 0. For factually
  sensitive scenes with named entities, the entity's *distinctive* tokens
  (location words excluded) or an alternate term must appear in the caption:
  confirmed 1.0, partial .6, weak .45, unnamed .15, no metadata .3.
  Period by archival wording only ×0.75; replica/costume/toy/lookalike
  markers ×0.3.
- Hard requirements: semantic ≥ 0.38, factual ≥ 0.40, existing semantic
  rejection, no same-asset repeat in an unrelated adjacent scene. Any reason
  rejects; rejected rows sort last and the scene gate refuses them with
  `judge_<reason>`.
- Output per candidate: `scores`, `final_score`, `reject`, `reasons`,
  `confidence`, `weak`, `factual_notes`, `diversity_notes`, `geometry`,
  `rationale`. Stored in `media.relevance.judge` and in candidate evidence.
- Optional VLM (`VISUAL_JUDGE_VLM_PROVIDER=openai`, default `none`): top 3 per
  scene, 24 per project, low-detail preview image, structured 0–10 scores. It
  can lower scores or veto (`vlm_wrong_subject`, `vlm_watermark_or_text`,
  `vlm_reject`); it never resurrects a deterministic rejection.

The judge applies to fresh acquisition. Persisted, reused and user-selected
assets keep their established destination authority (no silent replacement on
reopen).

## Continuity / diversity

Neighbours are the two previous scenes and the next one. Same asset in an
unrelated adjacent scene → novelty 0 (rejected); same creator with ≥ 50 %
caption overlap → "near_identical_shot" 0.3; similar concept 0.5. Same block
or a shared planned subject is intentional continuity (no penalty, continuity
1.0). Style family (archival / space / generated / graphic / modern) mismatch
with the adjacent scene lowers continuity and is noted as `style_jump`. The
existing `prefer_useful_novelty` tie-break still runs first.

## Transformation plan

`scene.visual_transform`: geometry (aspect, retained share, upscale), reframe
(`native_vertical` / `smart_reframe` / `native_frame`), steps with
`applied` flags, `motion_pattern`, `valid`, `failures`.

- Renderer-applied: subject-aware 9:16 crop (existing smart crop) and the
  Ken Burns **move**: archival/evidence → push-in, wide landscape (< 50 %
  retained) → pan, hook → push-in, payoff → pull-out, diagram/card → static;
  a scene continuing the same base always gets a different move than the
  previous cut. Speed remains the pacing value in `scene.motion`.
- Planned, not rendered (marked `applied: false`): subject tracking for
  landscape video (falls back to a static subject focal crop), 2.5D parallax
  for generated hooks, outpainting (never for real evidence).
- Invalid: effective resolution after reframe too low, or < 20 % retained.

## AI fallback rules

- Never generated: real, named people (`factual_sensitivity: real_person`).
- Historical events: generated only as a clearly illustrative painted
  reconstruction (no fake archival photo; verification uses "an illustration of …").
- Generate *before* accepting real media only when the best accepted real
  candidate is weak (semantic < 0.50, abstract < 0.55, final < 0.55, or
  OpenCLIP below the existing strong margin 0.26), the scene is not factually
  sensitive, and generation is permitted (chain, budget, circuit breaker).
  The weak real candidate is kept if generation fails.
- No safe real candidate at all → existing chain unchanged.
- `media_search.ai_fallback` records `prefer`, `reason`, `outcome`.

## Final quality gate (render admission)

Per scene: visual exists, rights usable (or valid generated/graphic
provenance), recorded judgement for this destination meets the floors and is
not rejected, short side ≥ 480, no later unintended repeat of an earlier
scene's asset, valid transformation plan. A failing, non-user-owned scene goes
through the existing permitted fallbacks (never "repaired" with itself). If
none exists the admitted visual is kept and reported as
`kept_failing_quality_gate` — render is not newly blocked. Results:
`scene.visual_gate`, `state.visual_quality_gate`.

## Observability

`diagnostics/visual-acquisition.json` now also carries per scene
`visual_query_plan.search_plan`, `media_search.routing`, provider
`query_sent`, candidate evidence `judge` scores, `media_search.judge`
(evaluated / rejected / reasons / top 5 with rationale),
`media_search.ai_fallback`, `visual_transform`, `visual_gate`; project-level
`visual_judge` (VLM provider/calls) and `visual_quality_gate`. No UI change.

## Tests

`apps/api/tests/test_visual_sources_retrieval_v2.py` (62 deterministic,
offline cases): routing, domain classification, planning, protection,
narration-fallback borrowing, Flickr license allow-list/normalization/
malformed/invalid key, registry gating, pre-filter, geometry, factual/semantic
rejection, ranking, ties, adjacent diversity, VLM bounds/veto/no-resurrection,
AI fallback table and integration, unavailable-source widening,
transformation plans, renderer move, final gate replacement / keep / repeat.

## Remaining limitations (need real local runs)

- Real provider responses: Flickr licence fields and Openverse/LOC/Europeana
  phrasing quality are only contract-tested with fixtures.
- Entity confirmation depends on captions; correct images with empty or
  foreign-language captions can be under-scored (alternate terms help).
- OpenCLIP weights are mocked in tests; real score distributions decide how
  often the "weak" path triggers generation.
- The VLM judge is untested against the live Responses API image input.
- Subject tracking, parallax, subject isolation and outpainting are planned
  only; landscape video still uses one focal crop.
- More generated images may be requested for non-factual weak scenes; the
  existing 3-per-project budget still bounds cost.

## Mac retest

1. `git fetch && git checkout cloud/visual-sources-retrieval-v2`, restart API,
   worker and web. Optionally set `FLICKR_API_KEY`, and
   `VISUAL_JUDGE_VLM_PROVIDER=openai` for a VLM run.
2. Generate NEW projects: Mars sky, Berlin Wall, chewing gum, fridge, a named
   landmark and an everyday action.
3. In `diagnostics/visual-acquisition.json` check: `search_plan.facets` and
   `source_queries`, `routing.domain`/`skipped`, `query_sent` per provider,
   `judge.top` rationale and rejection reasons, `ai_fallback`,
   `visual_transform`, `visual_gate`.
4. Watch the render: no Memorial-Church-for-Wall, no themed Mars stock, no
   repeated adjacent shot, archival stills push in, wide stills pan, consecutive
   cuts on one base use different moves.
5. Reopen, Change Media, Apply, re-render and export: locked choices persist,
   gate never blocks a previously renderable project.
