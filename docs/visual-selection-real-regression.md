# Real visual selection regression — evidence and correction

## Located project and version evidence

The read-only source is `apps/api/clipforge.db`, project
`184295fd-5767-4203-b771-08b557ccbc59`, title
“Warum kann man Kaugummi kauen ohne dass er zerfällt”.
It was created at `2026-10-01 20:00:44 UTC` (22:00:44 Berlin).
Revisions are 1 (initial), 2 (render), 3 (export), 4 (exported media cleanup).
Revision 2 records a 32.17-second render and the two exact windows below.
Project data was read without changing any revision, file or live job.

The checkout reflog records `bfab898` at 21:57:23 Berlin, before creation.
Selected assets contain the Phase-1 rights policy, provider-terms evidence,
canonical keys and destination intent fingerprints introduced by that commit.
Thus Phase-1 code was active; no build/worker SHA is stored in project state,
so the running process's exact SHA cannot be independently recovered.

The original asset files, rendered MP4 and Critic frames were cleaned after
export. Search diagnostics also disappeared from revision 2 because
`services._apply_render_result` calls `pipeline._build_scenes`, which did not
preserve `media_search` or `visual_query_plan`. This limits recovery of exact
request counts, full candidate pools and raw relaxed verifier results.

## Both playback occurrences

Both show Pexels photo `33159752`, by **PIC MATTI**:
[source page](https://www.pexels.com/photo/urban-street-scene-with-bold-german-billboard-33159752/).
Title and description both read:
“Woman smokes beside a striking 'Die fetten Jahre sind vorbei' billboard.”
Tags are empty. Rights are usable under explicitly recorded Pexels provider-wide
terms, policy `clipforge-commercial-edited-v1`; this was a relevance failure,
not a rights failure.

### 12.000–14.200 seconds — `scene_04_02`, block `voice_block_04`

- Narration: “kann es Jahre dauern, bis Kaugummi zerfällt.”
- Visual goal: “kann Jahre dauern Kaugummi zerfällt”. Intent objects are
  `kann`, `Jahre`, `dauern`; no actions/context; source `narration_fallback`.
- Queries: `kann jahre dauern kaugummi zerfällt`,
  `kann jahre dauern kaugummi zerfällt bis`, `kann kaugummi kauen zerfällt`.
  The candidate records the **second query** as its origin.
- Metadata relevance: **30**, confidence `acceptable`, tier **3**.
  Only `jahre` matches. Subject matches are empty. Query provenance is true;
  metadata/query disagreement is false because that same word agrees.
- OpenCLIP: actually verified on `provider_thumbnail`, one image;
  combined **0.2845419243**, scene **0.3059822023**, subject **0.1630470157**;
  presentation risk false. There was no unavailable-verifier fallback.
- Director: `ACCEPTED_REAL`, `stock_photo`, `real_media_staged_search`.
  Asset status `photo_ready`; no continuity/reuse marker or source scene.
- The existing quality gate accepts the persisted verdict as `visual_verified`.
  The unverified metadata alone also passed as `metadata_match`.
- Final Critic: rendered scene score **0.271**, required **0.24**, subject
  **0.1053**, semantic rating `good`, despite presentation risk true.
  Its remaining findings concern drawn text/overlay dominance.

**Cause:** `media_relevance` treated query provenance plus the same incidental
metadata word as independent corroboration. That disabled its single-word
rejection and promoted the asset to tier 3. A passing scene similarity then
qualified it; the low subject score had no independent rejection authority.
This selection used staged search, not relaxed search. The lost diagnostics
prevent an exact pool/exhaustion reconstruction; no persisted evidence supports
claiming that a lack of better candidates lowered this scene's threshold.

### 21.133–23.667 seconds — `scene_07_01`, block `voice_block_07`

- Final narration: “Rutscht er beim Kauen aber zu weit in”. The original
  complete beat was “Rutscht er beim Kauen aber zu weit in den Rachen,
  löst er den Schluckreflex aus.”
- Visual goal: “Rutscht beim Kauen aber weit Rachen”. Intent objects are
  `Rutscht`, `beim`, `Kauen`; no actions/context; source `narration_fallback`.
- Queries: `rutscht kauen weit rachen den löst`, `kann kaugummi kauen zerfällt`.
  The candidate records the **first query** as its origin.
- Metadata relevance: **−65**, confidence **`rejected`**, tier **0**.
  Subject/local matches are empty. Query provenance and disagreement are true.
- OpenCLIP selection result: **not persisted**. The old relaxed path called
  the verifier directly and returned only a scalar, losing the result object.
  Under `bfab898`, a candidate with this rejected evidence can pass only via
  that direct positive verdict and the gate's strong-score override. Therefore
  its scene scalar necessarily reached **at least 0.26**; this is a code-path
  inference, not a recovered exact score. An unavailable/exception verdict
  cannot admit this rejected metadata. The staged shortlist excludes it.
- Persisted fallback stage: `real_media_only_relaxed_fit`.
  Director: `ACCEPTED_REAL`, `stock_photo`, `real_media_relaxed_fallback`.
  Asset status `photo_ready`; no continuity/reuse marker or source scene.
- Final Critic: scene **0.275**, required **0.26**, subject **0.1463**,
  semantic rating `good`; repetition excused as `overlay_progression`.

**Cause:** when staged search had no accepted result, relaxed search could
directly verify metadata-rejected candidates. `real_media_quality_gate` let a
strong scene scalar override semantic rejection. It also substituted that scalar
for missing combined evidence, and the complete visual verdict was never saved.

## Acquisition order, repetition and Phase-1 reuse

Playback order is not acquisition order. Revision 1 has `scene_07_01` but no
`scene_04_02`. The poster's recorded fingerprint for scene 7 exactly equals the
fingerprint recomputed from revision 1's full swallowing beat:
`9ef30c553a1e9dff8a3dad941c39e7251b453d96ffc6cfb428695b5cd28f97ea`.
The initial Critic history has nine scenes; the final render has twelve.
There is no scene-7 replacement/borrow action in its repair records.

The original relaxed scene-7 selection therefore preceded the new earlier
scene-4 selection. Measured retiming added scene 4's second part; the Critic's
existing repair/media pass fills missing retimed scenes. `prepare_project_media`
tracked used assets only as it visited scenes. Scene 7's existing asset was
not reserved when the new earlier scene 4 was searched, allowing a fresh
provider selection of the same identity. The shared file path does not prove
renderer borrowing: both records have independent query/acceptance fingerprints,
fresh-selection statuses and their own Director search decisions.

**The Phase-1 destination reuse gate did not participate in either poster
selection:** these were fresh provider admissions. Its ordinary cache shortcut
also admitted already-owned files without destination relevance checks.
The Critic then excused the cross-fact duplicate merely because the overlays
differed and its scene score passed. These were two distinct acceptance holes.

## Other weak visuals

- **Teeth/mouth:** Commons `24060531`, “Mouth and gum damage”, describes damage
  from smokeless/chewing tobacco, not gum material. Both its degradation beat and
  saliva reuse have no local metadata matches. Old-gate replay accepts them via
  `topic_metadata_match`; the destination reuse gate **did participate** for the
  saliva reuse and admitted this weak topic evidence. The new gate rejects both.
- **Another related failure:** Commons `12118725`, “Chewing gum pub dublin
  20090925”, describes a Dublin pub notice about disposed gum. It also entered
  through relaxed fit with topic-only metadata and no persisted source vision.
  The revised gate rejects that same proven mechanism.
- **Heat/moisture object:** generated image `e35a9f007f24b39ee7ba`, not real
  stock. Its existing AI-image verification records combined **0.25096**, scene
  **0.24185**, accepted at **0.24**. The Critic reports framing/motion trouble
  and exhausted generation budget. This is a separate generated-image quality
  issue; no AI-image system or threshold was changed.
- **Later face/swallowing:** Pexels video `4273390`, a woman with cake in her
  mouth, query `kann kaugummi kauen zerfällt`. It has independent source-frame
  verification: three frames, combined **0.25080**, scene **0.25717**, subject
  **0.21475**. This remains an independently verified but weak conceptual fit;
  its exact evidence does not prove the poster's semantic-rejection bypass.

## Correction and retest

The existing deterministic authority now requires independent corroboration of
a single incidental metadata word. A concise explicit local subject remains
valid. Rejected semantics cannot be rescued by a strong scalar; unverified
query provenance and topic-only metadata cannot grant permission to select.
OpenCLIP rejection remains authoritative with unchanged thresholds.

Relaxed search uses the existing shortlist verifier, preserves its complete
result, and cannot lower acceptance when the pool runs out. Cached acquisitions
revalidate destination relevance; the renderer rejects explicitly rejected
admissions and still uses the shared destination gate for borrowing. All other
scenes' asset/source keys are reserved before fresh acquisition, including later
cached scenes. Explicit fitting same-block continuity remains available.

Changing an overlay alone no longer excuses a duplicate across unrelated facts.
Retiming retains query-plan/search diagnostics; selected media records the gate's
actual acceptance reason. Rights authority and evidence remain unchanged.

The existing Critic may roll back a bounded repair render when unfixed rejected
assets cannot be re-admitted. It keeps the already completed historical video
and reports `repair_render_failed`; it does not count rolled-back changes as
successful or select unrelated filler. No second judge or Director is added.

On the Mac, check out the correction and restart API/worker. Generate a **new**
project for the question with the same options, rather than exporting the old
cached MP4. Inspect the two beats and source acceptance/search diagnostics in
the saved revision. Test discovery/reopen and unrelated reuse, then fitting
same-block continuity. No automated regression calls a paid image API.
