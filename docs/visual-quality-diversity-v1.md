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

## Follow-up: shared intent and fallback audit (2026-10-03)

Scoped to the newest three real projects after `8befb6a`; no repository-wide
review or provider redesign. Database reads were read-only. Failed acquisition
snapshots were compared with persisted revisions and actual render layouts.

| Run | Project | Proven finding |
|---|---|---|
| Mars | `d0348bbf-d264-4fe1-8ac1-bc296891970c` | Revision 2, `scene_02_01`, 5.0–7.5 s selected an unrelated historical portrait. |
| Berlin | `97ba7fb7-5c5d-4e24-8449-dfd54153def2` | Latest failed acquisition: `scene_04_01`, economic consequence, missing at render admission. Revision 1 alone predates this failure; the failure evidence is in the acquisition snapshot. |
| Fridge | `4bc4b44f-133d-4a9c-a16f-43bbc84a0229` | Revision 2 completed, 40.6 s. Its `render_failed` acquisition file is older than the successful revision; do not report a current render failure. |

Mars narrated “Feiner roter Staub wirbelt in der Marsatmosphäre umher.” Its
`narration_fallback` intent treated “Feiner”, “roter”, “Staub” as objects.
Relaxation searched `feiner roter`. Wikimedia photo `161273097`,
“Porträt Anna Magdalena Zellweger-Etter”, contained **feiner** Schleier and
**roter** Halsschmuck in its description. Those TWO incidental modifiers
produced score 60 / tier 3 / high metadata confidence, despite no global
subject match. OpenCLIP was `unavailable_preview`, with no scene score;
`metadata_match` admitted it through `real_media_only_relaxed_fit`. Public-domain
rights were valid. The planetary contradiction detector had no recognized
negative setting marker in this portrait caption. NASA was routed but yielded
no normalized candidates in the retained staged evidence. This was a metadata
acceptance defect, not proof that source suitability defeated a better NASA
candidate. The exact source is Wikimedia Commons
`File:CH-KBAR_-_Porträt_Anna_Magdalena_Zellweger-Etter_-_KB-023593.tif`.

Berlin narrated “Das belastete den wirtschaftlichen Aufbau der DDR.” Its
fallback intent searched narration fragments and even used “belastete” as a
coverage target. A cached concrete translation already described an idle East
German factory worker beside unattended machinery. Retrieval never used it.
Three AI images had already been accepted; scene four therefore correctly hit
`project_budget_exhausted`. The provider budget was not exhausted (9/18
requests, 3/72 verifications, 0/12 downloads). Reuse did not establish a fit.
The scene's role was `evidence`, so its chain omitted the existing relation
graphic despite sharing fact_03 with the preceding flight-of-workers block.
The long source claim also failed deterministic relation extraction, whereas
the shorter fact-linked spoken blocks produce a validated relation.

Fridge's final layout used one Pexels clip (`9462939`), three generated stills,
and a graphic. The absent-minded open-fridge image `538c77f1ceb84a2595cf`
appeared at 4.9–6.867 s and again at 13.733–27.8 s. Body retrieval searched
German narration fragments, while concrete English translations were reserved
for generation/reuse. The three-image budget was spent. Admission's first-pool
fallback bypassed literal-query novelty ranking, but the retained destination
scores **do not justify forcing a different winner**: the repeated image
scored about .299–.301, alternatives .273–.291 and .244–.251. Semantic priority
therefore remains above variety; no penalty or budget was increased. One
same-block alternative also had a materially weaker destination score.

### Smallest shared correction

- Resolve the existing fact-to-visual translation **before** automatic search
  and cache admission for provisional intents. Persist the subject, action,
  setting and bounded query inputs in the existing scene intent. Reuse the
  same translation for generation and split-block continuity; authored/user
  directions retain authority. No new model, provider or judge.
- Provisional narration-word matches without independent subject evidence are
  insufficient for metadata-only acceptance. An unavailable verifier cannot
  legitimize the two-modifier portrait. Resolved intentions also reject
  narration-only matches absent from the intended visual. Rights, CLIP
  thresholds, setting/temporal contradictions and relaxed gates stay intact.
- Give facts split across linked script blocks access to the existing validated
  relation graphic irrespective of the role label. Never join unrelated facts.
  Upgrade the old automatic default chain at admission; explicit restricted
  chains and user locks are respected. No synthetic text-card path.
- Preserve the V1 novelty comparison guard: each target's coverage must match,
  not merely the sum. No diversity ranking, reuse threshold or render treatment
  was changed in this follow-up.

Read-only replay with local output in `/private/tmp` confirmed the saved Berlin
failure now reaches `graphic_ready` with **the same three billed generations**,
and the actual Mars portrait fails both fresh and cached admission. This replay
uses saved translations and policy evidence, not new provider results. A new
real generation remains necessary to assess stock coverage and visual variety.

Follow-up validation: **1,981 backend tests passed** (172 existing dependency
warnings), including **18 new shared-intent/fallback regressions**. The focused
media/rights/diversity/planetary/historical/fallback set passed 303 tests before
the final manual-selection guard; the full final run includes that guard and
its extra regression. Ruff and `git diff --check` passed. Frontend unchanged.
The two critic/repair compatibility regressions found during development were
resolved by requiring explicit `narration_fallback` provenance, rather than
inferring an emergency intent from the shape of a concise authored direction.
