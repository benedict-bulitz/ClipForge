# Planetary setting authority — real saved-state audit

Branch `cloud/visual-quality-diversity-v1`, verified base `6c51027`.
Scope: local visual intent, evidence, selection/admission, generated fallback,
Final Critic and rendering. No provider/API changes, new judge or paid calls.

## Real project and evidence boundaries

Project **327f96b5-9dfb-438d-ac9b-08c86a8c5b1c**, exact question
“Warum ist der Himmel auf dem Mars rot?”, created **2026-10-04 09:42:38.698184**.
Current revision **4**, exported; the render was made at revision **2** and its
layout survives revisions 3–4. Duration **24.16 seconds**.

Database and revisions were read without writes. The runtime commit is **not
recorded**, so the exact generator SHA cannot be independently verified. The
checkout is at 6c51027, and retained acquisition evidence rejects candidate
`pexels:photo:19288551` for `earth_geography`: the previous setting fix did run.

The exported file survives as
`Downloads/warum-ist-der-himmel-auf-dem-mars-rot-055b1fafea.mp4` (9,290,242 bytes).
Export cleanup deleted the original asset, render, segment and critic caches.
Replay therefore uses **actual exported frames**, not re-downloaded or recreated
provider assets. It does not establish what a new provider preview will show.
No saved project was changed. The accompanying `planetary-setting-authority-replay.json` contains selected evidence,
old critic decisions and the offline replay results, without credentials.

## Final render map

Times below come from `render.layout`, not approximate narration bounds.

| Scene | Render seconds | Final source |
|---|---:|---|
| scene_01_01 | 0.000–2.267 | Pexels video 38498382: city/desert sunset |
| scene_01_02 | 2.267–4.533 | Pexels video 35072752: Cappadocia sunset/vegetation |
| scene_02_01 | 4.533–7.100 | Pexels photo 8474500 |
| scene_03_01 | 7.100–10.967 | Pexels photo 38197878: red cloudscape |
| scene_04_01 | 10.967–14.833 | Pexels video 30871873: ocean sunset |
| scene_05_01 | 14.833–18.067 | Pexels video 8474682 |
| scene_06_01 | 18.067–21.300 | Generated 27179b3bc7d8fb6ab7a6 |
| scene_06_02 | 21.300–24.160 | Same generated asset, block continuity |

### City: scene_01_01

- Narration: “Auf dem Mars bleibt der Himmel sogar”; scene_01_02 completes
  “hell, wenn die Sonne schon weg ist.”
- Intent (`triple_hook_v2`): wide rocky red horizon after sunset, warm dusty
  twilight glow, dark ground versus glowing sky. The positive visual description
  omits Mars, although narration/block context establishes the planetary setting.
- Provider **Pexels**, video **38498382**, creator **Yunus Terk**.
- Source: https://www.pexels.com/video/stunning-desert-sunset-over-remote-landscape-38498382/
- Winning query: `desert twilight sunset horizon`.
- Title, description and tags empty. Source slug says desert/sunset/remote
  landscape; **it does not expose the buildings**. Exported frames clearly do.
- Deterministic score **76**, high, tier 3; matches desert/landscape/sunset.
- Provider OpenCLIP: scene **.303790**, subject **.284389**, overall **.298991**,
  three frames. Old setting evidence: required=true, mismatch=false, no markers.
- Fresh staged winner, `visual_verified` by `real_media_quality_gate`,
  `ACCEPTED_REAL`. First-query strong coverage stopped search; not cached/reused.
- Old Final Critic: **GOOD**, rendered score **.2707**, requirement **.24**.
  It tested positive fit, with no environment contradiction comparison.

The second opening clip is independently **Pexels video 35072752**:
https://www.pexels.com/video/sunset-over-cappadocia-s-majestic-landscapes-35072752/
Same winning query, empty title/description, score **60**, high/tier 3,
OpenCLIP scene **.276032**, old critic **.2862**/GOOD. It is another fresh
selection, not reuse. Its visible scrub/trees provide the visual conflict;
this fix adds no Cappadocia or other location-specific exception.

### Red cloud: scene_03_01

- Narration: “Die Staubkörnchen streuen das Licht der Sonne und erhellen dadurch
  den Himmel.”
- Intent (`fact_translation`): reddish airborne dust, sunlight scattered into an
  orange-red glow around the low Sun, barren rocky Martian desert/hazy atmosphere.
- Provider **Pexels**, photo **38197878**.
- Source: https://www.pexels.com/photo/dramatic-red-sunset-cloudscape-38197878/
- Query: `himmel auf dem mars rot`.
- Caption: “Stunning cloudscape with vibrant red hues during sunset, creating
  a dramatic sky view.” No identified Earth location or incompatible structure.
- Deterministic **56**, acceptable/tier 1, query disagreement recorded;
  OpenCLIP scene **.255216**, subject **.161721**, overall **.241192**, thumbnail.
- Required setting=true, mismatch=false; fresh staged `visual_verified`.
- Old critic semantic **GOOD**, **.2765** against **.26**. A separate motion/
  subject issue was recorded; it did not establish an Earth-setting conflict.
- Classification **AMBIGUOUS**: neither saved metadata nor inspected frames
  establish Earth provenance. Clouds/red/sunset alone are not grounds to reject.

### Ocean: scene_04_01

- Narration: “Das passiert sogar noch, wenn die Sonne schon hinter dem Horizont
  steht.”
- Intent (`fact_translation`): dusty Martian horizon after sunset; red suspended
  dust glowing across the dim orange-brown sky; rocky Mars desert at twilight.
- Provider **Pexels**, video **30871873**.
- Source: https://www.pexels.com/video/serene-ocean-sunset-with-vibrant-sky-30871873/
- Query: `dusty martian horizon sunset the sun`.
- Title/description/tags empty, but **the source slug explicitly says ocean**.
  Exported frames independently show open water and waves.
- Deterministic **76**, high/tier 3; sky/sunset matches, no subject matches.
- OpenCLIP scene **.308489**, subject **.148408**, overall **.284477**, three frames.
- Required setting=true but mismatch=false: the previous metadata authority
  lacked an open-water conflict class. Fresh staged `visual_verified` selection.
- Old critic **GOOD**, **.3133** against **.26**, no scene issue.

All these real selections had usable Pexels rights: provider-wide Pexels terms,
commercial use/modifications allowed, no attribution requirement, acceptance
policy `clipforge-commercial-edited-v1`. Rights were not the cause and are unchanged.

## Common cause and correction

The prior authority repeatedly inferred a planetary requirement from loose
text, then applied a limited **metadata-only** veto. Neither provider OpenCLIP
nor rendered-frame Final Critic had a structured environment constraint or a
separate contradiction verdict. The critic returned GOOD on positive similarity
before revisiting negative metadata. Generated media and fresh low-level render
admission also had paths that did not consult the setting authority.

The correction keeps the same authorities:

1. Persist `scene.required_environment`: version, planetary domain/entity,
   actual representation, incompatible observable environments and context key.
   Derive it from scene/block/facts; refresh after edits. Queries do not establish
   it. Carry it through positive prompt wrappers and destination acceptance keys.
2. Share four bounded evidence classes: built environment, road transport,
   vegetation and open water. Applicability follows the actual setting; explicit
   Earth comparisons, analogues and requested ancient-water scenes retain their
   exceptions. No ban on blue/clouds/desert/rocks/sunset, and atmospheric “waves”
   alone do not establish liquid water.
3. Extend the **existing local OpenCLIP verifier** with contrast against those
   environments. Full image plus at most three deterministic side/lower views
   share cached embeddings. This exposes small background structures that centre
   crop/captions can hide. Existing .24 scene threshold remains unchanged;
   contradiction additionally requires a .04 margin over the actual-setting
   comparison. Video needs a majority of the existing three samples.
4. Persist the separate contradiction evidence. High positive scores, novelty,
   source identity, query provenance and continuity cannot cancel it.
5. Final Critic checks metadata/persisted conflicts before a positive score,
   evaluates the same constraint on rendered frames and persists visual conflicts
   for repair/reuse/admission. Its existing wrong-media repair path handles them.

This is bounded similarity evidence, not proof of planetary authenticity.
Unknown metadata and unavailable vision still follow the existing acceptance/
fallback policy; a missing observation is never fabricated as a negative or a
positive. No second judge/model, extra provider call or paid generation in audit.

## Authority by path

| Path | Before | After / can a known applicable contradiction bypass? |
|---|---|---|
| Initial/provider shortlist | Limited metadata veto; positive CLIP only | Metadata + structured visual verdict; **no** |
| Staged selection | Same limited gate | Same shared gate before novelty; **no** |
| Relaxed selection | Shared metadata/positive threshold | Same contradiction veto, loose pass cannot override; **no** |
| Cache/saved replay | Recomputed metadata; old positive scores possible | Recompute metadata, retain scoped contradiction, bounded local legacy upgrade; **no** |
| Reuse/continuity | Destination relevance, limited setting evidence | Destination constraint checked before relation/continuity shortcuts; **no** |
| Generated media | Positive local score / existing unavailable policy | Same constraint in generated verification; persisted conflicts vetoed; **no** |
| Deliberate graphic | Existing intentional explanatory-graphic policy | Same policy; graphic is not an actual-setting photograph; no text cards added |
| Final Critic repair/replacement | Positive score could return GOOD first | Metadata/visual veto before GOOD; cross-scene scores cannot override; **no** |
| Final renderer admission | Full completion gate, conditional low-level gate | Completion handles fallback; unconditional known-setting veto even for fresh low-level admission; **no** |

Legacy local checks consume the existing per-scene verification budget, restored
before final admission. No search fan-out change; generated fallback retains its
existing project/scene budget. Fallback completion precedes normal rendering.
If every permitted fallback is exhausted, the existing hard failure remains.

## Real replay

Offline cached `ViT-B-32 / laion2b_s34b_b79k`, existing Final Critic sampling
(start inset/middle/end inset), actual exported video frames. No network.

| Asset | Metadata conflict | Visual conflicting frames | New critic | Saved admission |
|---|---|---:|---|---|
| City 38498382 | None retained | **3/3**, built environment | POOR | **REJECT** |
| Opening 35072752 | Not established by current geography matcher | **3/3**, vegetation | POOR | **REJECT** |
| Red cloud 38197878 | None | 0/3 | GOOD positive fit | **AMBIGUOUS** setting; allowed by existing policy |
| Ocean 30871873 | **Open water** | **3/3**, open water | POOR | **REJECT** |
| Generated planetary control | None | 0/3 | GOOD | No setting veto |

The exported state's generated control has no cache path after cleanup, so raw
file admission is unavailable for that control independently of setting. Its
pixels and critic setting result pass. Replay does not regenerate the project
or assert that unseen future candidates will always be classified correctly.

## Validation and retest

Final validation: **258 focused tests passed** (107.03s), including **28 new
setting-authority cases**; **2,037 backend tests passed** (199.91s, 172 existing
deprecation warnings). Ruff and `git diff --check`: PASS. All tests use mocked
external providers; the real-model replay is separate from the test suite.
Frontend unchanged; frontend tests/typecheck not applicable. The test-created
`:memory:.ses` artifact was removed. No merge to test/main.

Regenerate **only**: “Warum ist der Himmel auf dem Mars rot?”
Inspect the opening and sunset explanation for city/vegetation/open-water
substitutes. If no acceptable stock remains, verify that the existing AI/reuse/
graphic chain completes. In diagnostics inspect `required_environment` and
`setting_evidence`, including the Final Critic's `setting_authority` verdict.
