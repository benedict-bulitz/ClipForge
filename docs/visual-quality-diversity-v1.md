# Visual quality and diversity V1

Base: `test` / `origin/test` `9ea2b7d`. Work branch:
`cloud/visual-quality-diversity-v1`. No integration merge.

## Read-only real-project audit

Queried the local `apps/api/clipforge.db` in SQLite read-only mode on
2026-10-03. Chose the newest project by exact original question. Inspected
revision 2 scene state, translation/generation records, search diagnostics,
and actual final `render.layout`. Layout is the authority for what appeared:
retiming/final admission/critic repair can change the scene state after search.
A stale acquisition diagnostic or null media in an unused split is not proof
that the completed video had missing media.

| Question | Latest project | Created | Actual layout |
|---|---|---|---|
| Warum ist der Himmel auf dem Mars rot? | `ea87a9b4-1cc3-421b-9c3d-6939b18ef0f6` | Oct 3 12:36:34 | 29.13 s; one stock still, three generated stills |
| Warum öffnen wir den Kühlschrank, obwohl wir keinen Hunger haben? | `a51acf51-5cdd-468f-8a63-0b371148b076` | Oct 2 14:46:55 | 37.00 s; one stock clip, two generated stills |
| Warum kann man Kaugummi kauen ohne dass er zerfällt? | `9bb5c54d-29b2-4511-b70e-08464b3bdad9` | Oct 2 14:54:52 | 24.85 s; one stock clip, three generated stills, one graphic |
| Warum wurde die Berliner Mauer gebaut? | `13e58bf0-bc1a-4cbe-8a33-588f2e78bbe3` | Oct 2 14:40:36 | 37.77 s; archival-looking stock/AI, graphics, weak modern geographic B-roll |

Only Mars is newer than the previous Phase-2 audit. Do not describe the older
13.3-second gum video as the newest project. The retained Mars MP4 was sampled
at 2, 12, 20 and 26 seconds; the 20-second sample confirms the reused red sky /
rocky plain still. No provider network calls or paid generation were used.

### A/B — missing concrete intent and collapsed concepts

Non-hook intent source counts: Mars 8 narration fallbacks, fridge 10, gum 6,
Berlin 12. The fallback planner extracts bounded narration words; it does not
invent a visible mechanism. `_build_scenes` preserves fact/block identity and
`_aligned_visual_intents` aligns supplied plans by fact. Where no valid plan
survives, the emergency intent is explicitly marked `narration_fallback`.
These states demonstrate missing concrete direction, not a proven failure to
rank an existing rich intent.

For fridge, `voice_block_02` describes reward/availability; `03` the learned
visual cue; `04` automatic habit; `06` moving the reward to another location.
Yet the existing fact-to-visual translator produced:

- hand gripping fridge handle, partly open door;
- person reaching toward closed fridge handle;
- hand gripping fridge handle and reaching inside automatically;
- hand opening fridge door;
- hand at closed fridge, basement stairs in the background;
- hand gripping fridge handle again.

The translator received each statement plus canonical topic subjects, but no
preceding visual/statement. It repeatedly represented the topic's default
action instead of differentiating the visible contribution of each beat.
Search fell back to German narration fragments, not distinct English physical
concepts. Example: `dein gehirn speichert solche wiederholten abläufe`. Many
non-hook searches admitted zero candidates to verification. It is incorrect
to say a diverse accepted stock pool lost to a duplicate in this project.

Mars translations similarly converge on dusty plains and sky: airborne rust
dust, landscape beneath thin air, suspended dust/light, red-orange horizon,
sunset dust, blue halo around the sun. Some shared environment is factually
appropriate; a near-identical view for every mechanism is not an independent
new explanatory concept. The latest gum translations already progress through
stretching a gum piece, compression between molars, and resin/latex/polymer raw
materials. Retrieval must not replace those relevant concepts with random
variety.

### C/F — small usable pool and exhausted existing AI budget

Fridge final exposure: `91b60b843bcf89cb1987` 9.233 s,
`65d1bc5522ae9a17f416` 23.467 s, stock video `37416339` 4.300 s.
Three AI images were accepted under the existing budget; a third hand/closed
fridge view did not survive final admission/repair. The final rendered footage
therefore contains TWO repeated generated stills, not two retrieved clips.

Mars exposure: stock `8474500` 4.800 s; generated `209b66bd47f6abebd439`
16.430 s, `9d82efbf33f518afe7fa` 3.433 s, `7e387ab79b6661dd5f2e`
4.467 s. Later light/sunset beats reuse the first generated dusty-sky view.
Three accepted AI images exhaust the same project cap. Query diagnostics show
three logical queries on failed searches, typically 7–12 provider requests,
not exhaustion of the 18-request acquisition allowance. Scarcity / AI cap,
not a newly exhausted acquisition budget, explains later fallback reuse.

Gum exposure: stock `10551703` 5.567 s; stretching AI 3.333 s; molars AI
4.067 s; raw-material AI 5.200 s; the same final graphic 6.683 s.
That is five distinct assets. Its three-image AI allowance is spent. The
repeated final graphic is a separate composition/overlay issue; its persisted
cooling copy also differs from the gum-base explanation. No topic-specific
retrieval exception or unsupported variety was introduced to compensate.

### D/E — first-match tie bias, with necessary continuity

Automatic fresh searches already exclude used canonical assets. They do not
compare caption concepts with preceding scenes. Routed ranking orders by
selection tier, coverage, scene CLIP, deterministic metadata, kind and relative
provider rank. Thus a different file with the same concept has no tie cost.
`_related_media` previously kept the first item with the strongest focused
query overlap. Renderer emergency borrowing also visits eligible assets in
list order. These are proven code biases; the recorded projects do not retain
a diverse accepted alternative pool proving that a particular distinct asset
lost. The generic mocked regression isolates the tie mechanism.

Same-block photographic continuity is deliberate: a single script statement
may be split after TTS, while its information overlay evolves. It still uses
the destination gate. Do not force these fragments to have unique media.
Cross-block reuse remains necessary when all better real/AI options fail.

### G — presentation amplifies repetition but does not create the small pool

The renderer already uses bounded OpenCLIP crop analysis, focal-centered 9:16
geometry, focal-safe pan, push/pull alternation and 2x rendering. No new subject
model is needed. Still zoom was a fixed per-frame increment capped at 8%:
long stills reached the cap early and then froze; slow/short stills moved less.
Documents used the same fill crop / motion as photographs, potentially losing
page edges. These are deterministic presentation issues, independent of the
semantic retrieval authority. New motion cannot make one asset into a new
explanatory concept.

## Implemented scope and priority

- Existing script-planner guidance now requests supported progression across
  different facts, with deliberate continuity and factual setting constraints.
- Existing fact-to-visual translation receives at most the preceding two
  selected scenes' context (one preceding different statement, bounded caption
  and narration). It requests THIS statement's visible action/material/detail,
  without inventing diversity. It adds no model calls and retains the same
  per-statement/project and bounded process caches. Same-statement reuse stays.
- Accepted generated metadata persists known subject/action/environment facets;
  unknown facets remain null. Real-media concept evidence uses bounded captions,
  never provider identity, rank or query provenance. No embedding calls.
- Selection compares only already eligible rows with the same tier, kind and
  per-target coverage, within .01 scene-CLIP and four deterministic metadata points. A
  caption/concept repetition penalty is at most .008; it can break close ties,
  not override strong scene evidence. Metadata-only ordering changes only an
  actual metadata tie. The preceding two scenes bound history; same-block
  continuity is exempt. Routed, legacy staged and verified Commons paths share
  this helper. No widening/search/verification budget changes.
- Related reuse breaks only equal focused-relation/destination-evidence ties.
  It uses the existing deterministic relevance authority and destination-scoped
  verification, never another scene's CLIP score. Callers still validate rights,
  destination/reveal/repair/locks first. Renderer emergency borrowing remains
  last-resort; it is not a new diversity selector.
- Compact candidate and winner diagnostics retain previous concept, candidate
  known facets/terms, bounded penalty, continuity exemption and ranking reason.
  Generated/reuse evidence survives normal JSON revisions/manifests.
- Still motion keeps existing short-scene speed and treatment differences,
  but spreads capped movement over a longer duration. It stays focal-anchored,
  deterministic and <= 8%; repair and graphic motion retain their limits.
- Explicit document/map/manuscript/engraving/chart evidence uses a static
  contain frame with padding, preserving all page edges. These remain accepted
  real images; there are no synthesized text cards. Ordinary archival photos
  keep the existing restrained focal treatment.

No acceptance threshold, rights policy, provider/router, query count, AI
budget, judge, Visual Director, critic, factual setting protection or fallback
chain was replaced. Novelty cannot admit an ineligible candidate. Real Mac
retest is still required to assess the worker-model planning improvement;
mocked tests cannot prove the aesthetics of future generated media.

## Validation

The new tests cover semantic ties/superiority, concept/identity repetition,
intentional continuity, routed/legacy pooling, rights/metadata/CLIP rejection,
bounded acquisition, reuse tie precedence, translation/cache context,
persisted generated facets, deterministic duration-aware motion, portrait /
landscape focal retention, and actual local MP4 document-edge retention.
Existing planetary, historical, fridge, gum, rights, fallback, renderer and
critic regressions are included in focused and full backend validation.

Final validation on the feature tree:

- Full backend AFTER the per-target coverage guard: **1963 passed**.
- Focused visual/provider/reuse/renderer/critic regressions: **572 passed**;
  targeted coverage-guard/selection validation after the final change: **207 passed**.
- **28 new quality/diversity tests** are included in the full suite.
- `ruff check .`: PASS. `git diff --check`: PASS.
- Frontend and frontend typecheck: not affected; no frontend files changed.

## Real Mac retest

Generate NEW projects with these exact questions:

1. Warum öffnen wir den Kühlschrank, obwohl wir keinen Hunger haben?
2. Warum kann man Kaugummi kauen ohne dass er zerfällt?
3. Warum ist der Himmel auf dem Mars rot?
4. Warum wurde die Berliner Mauer gebaut?

Compare narration-linked concepts, not just distinct filenames. Check
`diagnostics/visual-acquisition.json` candidate/winner diversity evidence;
confirm repeating same-block fragments are intentionally exempt, unrelated
alternatives still fail, generated-image spend remains bounded, and documents
retain page edges without moving text. A relevant repeated view remains the
correct outcome when no comparably valid distinct visual exists.
