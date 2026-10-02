# Missing-media regression after 07f86ff

## Evidence recovered from the real Mac

Project `e7f42c67-a334-44e7-95fb-11bbc36ef63a`, created October 2, 2026
at 06:33:53 UTC, is the newest project for
“Warum kann man Kaugummi kauen ohne dass er zerfällt?”.
Job `2f141dc3-2a1e-4c7d-b026-6be7a282cf88` failed at 06:36:37 UTC:

- Stage: rendering / Preparing scene video.
- Completed scenes: **1 of 5**.
- Completed stages include media, review, voice, and alignment.
- Media acquisition took 143.965 seconds.
- Error: “No real scene media is available. Retry media discovery; text cards are disabled.”

The renderer encodes scenes sequentially and updates progress after each
successful segment. Therefore the failed segment is **scene_02_01**, block
`voice_block_02`.

| Field | Recovered evidence |
| --- | --- |
| Narration | Kaugummi ist eine weiche Masse, die du stundenlang kauen kannst, ohne dass sie zerfällt. |
| Visual goal | Kaugummi eine weiche Masse stundenlang kauen |
| Intent source | narration_fallback |
| Objects | Kaugummi; eine; weiche |
| Actions/context | Empty |
| Story role/stage | primary_answer / reveal |
| Preferred medium | video |
| Initial asset status | search_required |
| Persisted selected media | None |

Replaying the unchanged deterministic query/strategy planner on this revision
(including `_refresh_script_derivatives`) produces:

- `kaugummi weiche masse stundenlang kauen`
- `kaugummi weiche masse stundenlang kauen kannst`
- `kann kaugummi kauen zerfällt`
- Strategy: stock_video / concrete_subject_real_media.
- Chain: real_media → generated_image → reuse_previous_visual.
- No deliberate graphic spec is present for this scene.
- AI fallback is enabled; installation defaults are three billed automatic
  images per project and one automatic attempt per scene.

These are **reconstructed planner outputs**, not a recovered execution log.

The project has one cached Pexels video (`30301890`), one encoded first-scene
segment, cached narration, and these three readable generated PNGs:

| Generated asset | Sidecar summary | Original accepted scene score |
| --- | --- | ---: |
| 15948337c51d0eb2c93e | Pink gum and synthetic polymer pellets in a mixing bowl | 0.304978 |
| 5ff655348d94370ec3e6 | Intact gum on damp soil beside plants | 0.349602 |
| 5d430c93db18eaf6f764 | A person stretches/chews the same intact elastic gum | 0.258134 |

All three sidecars explicitly say verified, accepted, and no presentation risk.
All five scenes in the saved revision are initial search_required scenes;
none claims selected/continued with a missing cache. The four cached asset
files exist. The failed render's later scene statuses cannot be recovered.

## Limits of the historical trace

The database contains only revision 1, before acquisition. `_render_state`
works on a deep copy and commits a new revision only after rendering succeeds.
`generation.py` rolls back after failure and persists job progress/error, not
that working copy. Generated sidecars retain neither scene IDs nor the full
generation attempt log. The available backend log predates this run.

Consequently the exact stock rejections, relaxed-search verdicts, AI skip or
failure for Scene 2, run-time acquisition budget, pre-render asset_status, and
whether admission invalidated a selected asset are **unknown**. Three accepted
files do not establish the complete billing/attempt history. The repository is
at 07f86ff; the job did not record the running backend's Git SHA.

It would be incorrect to claim a proven historical A–H category, assign those
sidecars to particular scenes, or state that Scene 2 definitely exhausted its
AI budget. This change fixes a reproduced admission gap and retains evidence
for the next real run; it does not certify an end-to-end repair of this old run.

## Proven gap and correction

Before the change, final renderer admission could only use a source or borrow
another scene's media. It never completed the Visual Director chain when
admission found no usable source. Generated cross-scene reuse depended on
literal overlap between the focused query and the generated prompt summary.
English prompt summaries and German narration-derived queries can fail that
test even when a generated image fits the destination.

Final admission now completes the **existing** policy before voice/render:

1. Revalidate provenance, cache, rights, and scene acceptance.
2. Try the policy's permitted AI step under its existing project/scene limits.
3. Try permitted project reuse through `destination_asset_allowed`.
4. Try the policy's existing deliberate graphic step, if it has one.
5. Mark missing only when those permitted steps fail or a user lock blocks replacement.

For generated reuse without a focused lexical relation, the same OpenCLIP
generated-image verifier independently evaluates the destination, using the
existing fact-to-visual translation/prompt builder. Unavailable verification
cannot establish this fit. Scoped positive/negative evidence survives scene
media serialization and reopening; a negative verdict cannot be overridden
by changing the asset query. Reveal safety, locks, rejected identities, and
no_reuse run before verification. No real-stock threshold or rejection is
relaxed, and no synthetic text card is introduced.

The final pass performs no provider searches/downloads, restores the scene's
remaining acquisition verification allowance, preserves the generation outage
circuit breaker, and does not reset billed images or per-scene attempts.
Retiming retains strategies/constraints even for scenes without media.

`diagnostics/visual-acquisition.json` now preserves every scene's strategy,
queries, rejection counts/budgets, asset status, selected evidence, fallback
completion, and project generation records before rendering and on a render
failure. This is diagnostic evidence, not a new license authority or revision.

## Real cached-image check

The installed ViT-B-32 / laion2b_s34b_b79k weights were loaded offline on CPU.
Scene 2 was checked against all three real PNGs. The unchanged threshold is
0.24:

| Asset | Existing untranslated scene prompt | English diagnostic translation |
| --- | ---: | ---: |
| 15948337c51d0eb2c93e | 0.218512 | 0.207805 |
| 5ff655348d94370ec3e6 | 0.174352 | 0.236090 |
| 5d430c93db18eaf6f764 | 0.205487 | **0.266593** |

The English diagnostic was “a photo of soft chewing gum remaining intact
while a person chews it”. It was supplied only to this read-only investigation,
not added to production code or tests. The normal worker translation must
still succeed in the real retest. This proves an existing file can pass the
same destination threshold; it does not recover the old run's missing prompts.

## Regression coverage and retest

`test_visual_fallback_completion.py` covers strict stock rejection through
relaxed search, AI execution at admission, bounded attempts, generation
failure/rejection followed by graphics, lost caches, outage isolation across
the boundary, independently verified reuse, unavailable/negative verification,
continuity, all destination constraints, budget exhaustion, evidence reopening,
retiming, and a five-scene multilingual fixture. An actual FFmpeg segment is
encoded from a completed AI fallback. Paid APIs are mocked in tests.

Restart the backend on this feature commit and generate a **new** project:
“Warum kann man Kaugummi kauen ohne dass er zerfällt?”
Confirm all five scene sources exist, the irrelevant street/poster is absent,
the AI budget is respected, and the render completes. If it fails, retain that
project's `diagnostics/visual-acquisition.json` and job ID; they now expose the
precise fallback divergence rather than only the initial storyboard.

Validation: 23 new regression cases, 348 focused media/director/renderer/critic
tests, and the full backend suite (**1,761 passed**). Ruff and
`git diff --check` pass. No frontend files or frontend schemas changed.
