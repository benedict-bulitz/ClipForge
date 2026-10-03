# Final planetary-setting retest audit (2026-10-03)

The four newest exact-question projects were found in the local read-only DB.
Export revision 2 and its render layout identify what played; revision 4 is
export cleanup. Retained `audio-layers/v2/picture.mp4` files were sampled locally.
No provider requests, paid generation or project/revision mutations were made.
Acquisition JSON can predate the successful render: Mars/Kaugummi retain
`render_failed` snapshots, while DB `render.status=complete` and video prove
successful export. Those diagnostic statuses are not new generation failures.

| Question | Project | Created (stored timestamp) | Render duration |
| --- | --- | --- | --- |
| Warum ist der Himmel auf dem Mars rot? | aad06e30-0ebf-4104-b86a-9ec306a4c13e | 2026-10-02 14:34:01 | 26.33 s |
| Warum wurde die Berliner Mauer gebaut? | 13e58bf0-bc1a-4cbe-8a33-588f2e78bbe3 | 2026-10-02 14:40:36 | 37.77 s |
| Warum öffnen wir den Kühlschrank, obwohl wir keinen Hunger haben? | a51acf51-5cdd-468f-8a63-0b371148b076 | 2026-10-02 14:46:55 | 37.00 s |
| Warum kann man Kaugummi kauen ohne dass er zerfällt? | 9bb5c54d-29b2-4511-b70e-08464b3bdad9 | 2026-10-02 14:54:52 | 24.85 s |

## Roadside asset: persisted evidence

- Scene: `scene_07_01`, block `voice_block_07`, render **18.500–21.933 s**.
- Narration: “Regen, der ihn wegwaschen könnte, gibt es auf dem Mars nicht.”
- Goal: “Regen wegwaschen könnte gibt Mars nicht”; literal narration-fallback
  intent, explanation/final-payoff role. Objects: Regen, wegwaschen, könnte.
  Linked `fact_05` describes atmospheric dust, winds, lower gravity and absence
  of cleansing rain. No costume, sign or terrestrial demonstration was intended.
- Provider/identity: **Pexels**, `pexels:photo:5259405`.
- Source: `https://www.pexels.com/photo/man-in-a-space-suit-hitchhiking-5259405/`.
- Creator: T Leish, `https://www.pexels.com/@leish`.
- Title and description: **“A person in a space suit hitchhiking with a Mars
  sign on a roadside.”** Tags empty; dimensions 4480 × 6720.
- Winning query: `regen wegwaschen könnte gibt mars ihn`.
- Rights: Pexels License, `https://www.pexels.com/license/`; explicit
  provider-wide terms evidence, commercial use/modifications allowed,
  attribution not required, public domain unknown, policy
  `clipforge-commercial-edited-v1`. Acceptance: usable/established reuse rights.
- Metadata: score **16**, acceptable, one matched/local/global token `mar`,
  no scene-specific matches, selection tier **2**. Query provenance true;
  disagreement false. No presentation or temporal mismatch.
- OpenCLIP: provider thumbnail, one frame; scene **0.3021878600**, subject
  0.2827200890, combined 0.2992676944; presentation risk false.
- Gate: `real_media_quality_gate`, accepted **visual_verified**. Director:
  `ACCEPTED_REAL`, stock photo, `real_media_staged_search`, generation not needed.
- Reuse: fresh staged selection, `photo_ready`; renderer used the same identity.

The local frame at 20 s confirms the suit, cardboard sign, grass and paved road.
Final Critic also missed it: rendered-frame semantic score **0.3072**, required
0.26; semantic rating good, no issue IDs for this scene. Another similarity
judge or a higher similarity score would not establish the factual setting.

## Discovery, alternatives and why it won

| Query | Initial sources | NASA normalized results | Widening | Outcome |
| --- | --- | --- | --- | --- |
| regen wegwaschen könnte gibt mars | NASA + Wikimedia | 0; one request | Openverse/Pexels video, then Pexels photo | None accepted |
| regen wegwaschen könnte gibt mars ihn | NASA + Wikimedia | 0; one request | Same bounded tiers | Two roadside-astronaut photos passed |

NASA was actually queried twice, not merely routed. Its returned counts and
downstream rejection counts were zero; raw HTTP payloads were not retained.
This proves no normalized NASA alternative, not a guessed raw API-response
cause. NASA did not lose a comparison. Across the project, all recorded NASA
stages returned zero normalized candidates, including cached empty results.
The long German queries are visible limited-coverage evidence; query planning
is not changed by this acceptance patch.

Wikimedia returned zero. Openverse independently recorded rate limiting with
zero requests. Pexels returned 18 videos and 15 photos per query. Second-stage
dedupe excluded 26 previously seen/used identities. The only recorded accepted
alternatives were photos **5259405** and **5259414**, both roadside spacesuit
hitchhikers with signs, metadata score 16 and tier 2. Scene scores were
0.30218786 versus 0.28645542; the first won existing coverage/visual ranking.
Source suitability ordered discovery and supplied no acceptance/ranking bonus.

Recorded work: **2 logical queries, 8 search requests, 2 verification admissions,
1 download**. Limits: 3/18/72/12. Budget not exhausted. Strong coverage stopped
the third query (`himmel auf dem mars rot`).

The score was one `mar` match (12 local + 4 global). The canonical goal also
mentioned the planet, avoiding the global-only/single-incidental-match veto.
No setting-consistency check interpreted the contradictory caption. OpenCLIP
then passed the unchanged 0.24 threshold. Rights were correctly established;
this was a semantic failure. Query provenance was recorded but added no score.

## Small shared-gate correction

`planetary_setting_evidence` uses positive visual direction, narration and the
existing same-block/linked-fact context. Queries, exclusions and unrelated
story units cannot authorize themed exceptions. Explicit Earth comparisons or
requested costume/illustrative scenes remain possible.

Costume/themed/replica/merchandise/hitchhiking cues, terrestrial surroundings,
planet-lookalike captions and held props veto actual-setting acceptance.
Actor-plus-planet-label captions need physical setting evidence: a suit/name
alone cannot establish an environment. Genuine lunar mission captions with
soil/surface/craters remain eligible, including people. Negated surroundings
and rocks resembling faces are not treated as staged planetary settings.
Provider identity alone grants nothing.

The evidence sets existing deterministic confidence to rejected, so automatic
selection, relaxed search, Apply, cache, renderer/reuse and critic reuse retain
the existing quality/destination authorities. A high or persisted OpenCLIP pass
cannot resurrect rejection. Bounded candidate diagnostics persist the new
evidence. No new judge, suitability bonus, threshold, rights, query-count or AI
architecture change. Metadata remains evidence, not an authenticity certificate;
this does not claim to detect arbitrary deception absent descriptive evidence.

Offline replay with original persisted scores:

| Scene / identity | Evidence | Result |
| --- | --- | --- |
| scene_01_01 / pexels:photo:8474447 | desert described as resembling the planet | rejected |
| scene_02_01 / pexels:video:8474606 | astronaut running; planet name without environment evidence | rejected |
| scene_06_01 / pexels:video:8474638 | astronaut couple walking; planet name without environment evidence | rejected |
| scene_07_01 / pexels:photo:5259405 | hitchhiker/roadside/actor without environment evidence | rejected |

The roadside asset now fails same-scene cached admission and reuse with its
original score. This is offline gate replay, not a claim of real regeneration.
Absent acceptable stock, existing permitted AI/reuse/graphic fallback remains
responsible. Three earlier generated planetary-environment images exist in the
original render; tests prove AI fallback and fitting continuity without raising
generation budgets.

## Berlin ending: WEAK

At 28.333–31.733 s, scene_07_02 narrates “den Berliner Sektoren ab und ließ die
Mauer errichten.” Pexels photo **37119103** is a current Memorial Church view:
`https://www.pexels.com/photo/kaiser-wilhelm-memorial-church-in-berlin-37119103/`.
Caption: “Historic Kaiser Wilhelm Memorial Church in Berlin with scaffolding
and leafy foreground.” The sampled video confirms that view under wall labels.
It supplies geographic B-roll, not evidence of border closure/wall construction.

Metadata score 13, no local/scene-specific matches, topic matches berlin/memorial,
query disagreement, tier 1, scene score 0.31625184. The temporal gate requested
1961–1989 but accepted adjective `Historic` with no capture year. This is a
separate archival-evidence ambiguity. Historical policy tuning is not bundled
into the proven planetary/actor-setting correction.

## Fridge diversity: planning + coverage/budget + reuse

The final render contains one 4.3-second stock segment and **two generated
stills** for 32.7 seconds: 91b60b843bcf89cb1987 (9.233 s) and
65d1bc5522ae9a17f416 (23.467 s). These are not two repeating stock clips.
Local frames show refrigerator handles/interiors.

Visual translations repeatedly request a hand approaching/gripping/opening a
fridge in a home kitchen. A basement example adds a stairway but still centers
the fridge hand. Pexels/Openverse subsequently report throttling; Wikimedia
returns no acceptable alternatives. Physical acquisition limits generally are
not exhausted; many later searches admit zero verification candidates.

The configured three-image budget is exhausted after three accepted generations.
A third reaching-toward-handle image, 574ad501b3d9c3931e27, does not survive final
render admission/repair. Critic attempts record no accepted real alternative and
project-budget-exhausted generation. Scene_06_01 gets a fitting-base replacement;
render fallback and same-block continuity reuse the two fitting survivors.
Scene_06_02 records block_visual_continuity without search; later scenes record
render-admission reuse. Reuse is an actual contributor, not necessarily a
novelty-ranking defect.

No accepted distinct candidate demonstrably lost to a duplicate due to a weak
diversity penalty. Causes: repetitive planned subjects (A), limited accepted
coverage/provider throttling (B), exhausted AI budget and repair/continuity/reuse
(D). Shared acquisition budget (E) was not the principal cause. No proven C or
single retrieval-level diversity bug; no diversity/budget change implemented.

## Kaugummi diversity: latest saved render has distinct concepts

The newest exact-question project is **24.85 seconds**, not the earlier 13.3 s
run. It has five unique assets across six rendered segments:

| Time | Concept |
| --- | --- |
| 0–5.567 | sugar falling from spoon |
| 5.567–8.900 | gum stretched between fingers |
| 8.900–12.967 | gum between molars |
| 12.967–18.167 | resin, latex and polymer raw materials |
| 18.167–24.850 | existing process graphic twice |

Local frames confirm molars/materials. Three automatic generations are accepted,
then the budget ends and the final beats use the same graphic. Shared-block
timeline fragments extend their base intentionally. Remaining repetition is
constrained coverage, same-fact continuity and AI budget, not a distinct accepted
candidate losing ranking. The graphic concerns sugar-alcohol cooling rather
than the gum-base claim: separate planning/overlay evidence, outside this patch.
No artificial diversity is forced; no diversity fix implemented.

## Validation and retest

28 new generic setting regressions and 225 focused provider/router/LOC/history/
strict-selection/fallback/generic-media tests passed, including all 42 Kaugummi
mechanism regressions. Final full backend: **1935 passed**. Ruff and
`git diff --check` passed. Frontend unchanged.

Regenerate **Mars first** after restarting on the updated feature branch.
Inspect setting evidence/relevance rejects and verify actual planetary scenery
or permitted generated/reused scenery replaces themed stock. Confirm complete
fallback, unchanged bounds, no suit/planet-label shortcut and no text cards.
Then optionally regenerate the other three questions to compare coverage.
