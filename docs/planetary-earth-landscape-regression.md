# Planetary Earth-landscape regression

Branch: `cloud/visual-quality-diversity-v1`; base `265e99a`. Audit scoped to the
newest exact Mars question and the existing setting/admission authorities.
Read-only database access; no live provider requests or paid generation.

## Exact real trace

- Project: `f2eacfbf-59c3-4db0-b1d3-4aeefb0c415d`, created
  `2026-10-04 09:07:16.831898`, exported. Revisions 2–4 retain the final render.
- Scene: `scene_05_01`, block `voice_block_05`; actual render layout
  **14.033–17.290 s** (planned scene bounds 14.04–17.29).
- Narration: “Dadurch wirkt der Himmel von der Marsoberfläche aus rötlich.”
- Resolved intent: dusty Martian horizon beneath a reddish-orange sky;
  airborne dust gives the low sky a muted red glow; barren rocky Mars
  landscape viewed from ground level. Source: `fact_translation`.
- Provider: **Pexels**, photo **31661785**, creator **David Solce**.
- Source: `https://www.pexels.com/photo/stunning-red-rock-landscape-of-hanksville-utah-31661785/`
- Winning query: `barren rocky mars landscape viewed ground`.
- Caption/title/description: “Explore the mesmerizing red rock formations
  and desert landscape in Hanksville, Utah.” Tags empty; source-page slug
  independently repeats the geographic location.
- Rights: usable, `pexels-license`, commercial use and modifications allowed,
  attribution not required; evidence is explicitly **provider-wide terms**,
  accepted under `clipforge-commercial-edited-v1`.
- Deterministic score **62**, high confidence, tier 3; local matches
  `landscape`, `red`, with only `red` scene-specific. Query provenance true,
  disagreement false. The query did not itself add semantic permission.
- OpenCLIP: verified provider thumbnail, one frame; overall **.285794**,
  subject **.232323**, scene **.295230**. No presentation-risk rejection.
- Setting requirement **true**, but mismatch **false**, markers empty.
- Acceptance: `visual_verified`, `real_media_quality_gate`; fresh staged-search
  winner, decision `ACCEPTED_REAL`, stock photo, not cache/reuse/generated.
- Search: 3 logical queries, 11 provider requests, 29 verification admissions,
  one download; request/download/verification allowance not exhausted. NASA
  was routed but yielded zero candidates in the retained per-stage evidence;
  Commons also empty, Openverse rate-limited. Widening pooled stock.
- Novelty penalty **0**, reason `semantic_rank_preserved`; diversity did not
  override another semantic rank to admit this asset.
- Final Critic also accepted it: rendered-frame semantic score **.2611**
  against its unchanged **.26** requirement; no scene issue. This is further
  evidence that concept similarity does not establish physical setting.

The user's blue-sky/cloud observation is corroborated by the caption's explicit
Earth location; rejection does not require a new pixel-level detector. Local
asset copies were not available at the repository's default render locations;
the persisted source identity, acquisition evidence and render layout establish
the exact selection path without guessing its provider.

## Root cause and bounded correction

The planetary requirement was correctly established, but its metadata veto
recognized staged representations and a handful of Earth surroundings. It did
not recognize an environment explicitly located in an Earth country/US state.
Color/landscape matches and passing OpenCLIP scores therefore remained eligible.

Extend **only `planetary_setting_evidence`**:

- `earth_geography`: an environment noun followed by a locative phrase naming
  an Earth country or US state. Countries use the public-domain ISO country
  table bundled with the **existing `tzdata` dependency**; US states cover
  captions omitting the country. No geocoder, network lookup, new dependency,
  provider mandate, subject ontology or asset/topic exception.
- `terrestrial_sky`: a caption explicitly describes Earth/terrestrial sky or
  clouds. Mere blue sky, white clouds, desert, rock or red color do not trigger
  this veto.
- Existing negation handling, explicit Earth-comparison and deliberate
  analogue exceptions remain. A lab/credit location does not locate the
  depicted landscape. Unidentified geography remains ambiguous.

The existing authority turns a contradictory setting into rejected semantic
confidence before verification/ranking. Cache/reuse recompute that same
relevance, so old scalar acceptance and continuity/novelty cannot rescue it.
The actual saved asset now reports `earth_geography` and fails fresh quality,
cached admission and reuse. Existing generated/reuse/graphic fallbacks and
all thresholds remain unchanged.

## Validation

28 new generic regressions cover terrestrial locations and explicit sky
contradictions; cross-provider identity/rank/provenance; source-page evidence;
valid planetary capture, laboratory credits and negation; ambiguous clouds,
color and places; authorized comparison/analogue; novelty, cache, reuse and
relaxed-search authority; and completion through the existing verified AI
fallback. Focused planetary/diversity/shared-intent/historical/fridge/gum/
fallback tests: **173 passed**. **Full backend: 2,009 passed**, 172 existing dependency warnings. Ruff and
`git diff --check` passed. Frontend unchanged; no frontend/typecheck work needed.
The original `test` and `main` refs remain unchanged. Real Mac retest: regenerate
only “Warum ist der Himmel auf dem Mars rot?”.
