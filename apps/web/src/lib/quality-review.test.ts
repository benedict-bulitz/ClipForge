import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { appliedQualityRepairs, qualityReviewSummary, remainingQualityIssues, sceneQualityState, unresolvedQualityScenes } from "./quality-review.ts";
import type { FinalQualityReview } from "./types.ts";

const workspace = readFileSync(new URL("../components/project-workspace.tsx", import.meta.url), "utf8");

const review: FinalQualityReview = {
  version: 2,
  status: "issues_remain",
  summary: { label: "Repaired 2 scenes · 2 issues remain", repaired_scene_count: 2, remaining_issue_count: 2, warning_count: 0 },
  issues: [
    { id: "s11:semantic_match:wrong_media", scene_id: "s11", scene_number: 11, category: "semantic_match", code: "wrong_media", severity: "error", message: "Scene 11 does not show what the narration is about." },
    { id: "s11:visual_quality:text_heavy", scene_id: "s11", scene_number: 11, category: "visual_quality", code: "text_heavy", severity: "error", message: "Scene 11 is dominated by text." },
    { id: "s12:visual_quality:text_heavy", scene_id: "s12", scene_number: 12, category: "visual_quality", code: "text_heavy", severity: "error", message: "Scene 12 is dominated by text instead of a visual." },
    { id: "s3:visual_quality:low_contrast", scene_id: "s3", scene_number: 3, category: "visual_quality", code: "low_contrast", severity: "warning", message: "Scene 3 renders flat." },
  ],
  unresolved: ["s11:semantic_match:wrong_media", "s11:visual_quality:text_heavy", "s12:visual_quality:text_heavy"],
  repairs: [
    { scene_id: "s4", scene_number: 4, action: "replace_media", status: "applied", repair_attempted: true, repair_effective: true, result_message: "replaced a weak visual" },
    { scene_id: "s5", scene_number: 5, action: "adjust_composition", status: "applied", repair_attempted: true, repair_effective: true, result_message: "disabled unsafe camera motion" },
    { scene_id: "s11", scene_number: 11, action: "replace_media", status: "applied", repair_attempted: true, repair_effective: false, outcome: "unresolved", result_message: "no sufficiently relevant visual found" },
    { scene_id: "s12", scene_number: 12, action: "replace_media", status: "applied", repair_attempted: true, repair_effective: false, outcome: "unresolved", result_message: "the replacement remained text-heavy" },
  ],
};

test("quality review summarises the final video compactly", () => {
  assert.deepEqual(qualityReviewSummary(review), { label: "Repaired 2 scenes · 2 issues remain", tone: "attention" });
  assert.equal(qualityReviewSummary(undefined), null); // older projects show nothing
  assert.equal(qualityReviewSummary({ version: 1, status: "disabled" }), null);
  assert.deepEqual(qualityReviewSummary({ ...review, revision: 3 }, 4), { label: "Repaired 2 scenes · 2 issues remain (earlier render)", tone: "muted" });
});

test("repaired and unresolved are separate groups; a repair that did not help is unresolved", () => {
  assert.deepEqual(appliedQualityRepairs(review), [
    { sceneId: "s4", sceneNumber: 4, text: "replaced a weak visual" },
    { sceneId: "s5", sceneNumber: 5, text: "disabled unsafe camera motion" },
  ]);
  assert.deepEqual(unresolvedQualityScenes(review), [
    { sceneId: "s11", sceneNumber: 11, message: "Scene 11 does not show what the narration is about.", attempt: "no sufficiently relevant visual found", issueCount: 2 },
    { sceneId: "s12", sceneNumber: 12, message: "Scene 12 is dominated by text instead of a visual.", attempt: "the replacement remained text-heavy", issueCount: 1 },
  ]);
  // Notes that are not repair targets stay out of the unresolved list.
  assert.deepEqual(remainingQualityIssues(review).map((issue) => issue.scene_number), [11, 11, 12]);
  assert.equal(sceneQualityState(review, "s4"), "repaired");
  assert.equal(sceneQualityState(review, "s11"), "issue");
  assert.equal(sceneQualityState(review, "s3"), null);
});

test("workspace groups the review and links unresolved scenes to Change Media", () => {
  assert.match(workspace, /<QualityReviewPanel/);
  assert.match(workspace, /aria-label="Automatically repaired"/);
  assert.match(workspace, /aria-label="Unresolved"/);
  assert.match(workspace, /onFixScene=\{/);
  assert.match(workspace, /chooseSceneMedia\(sceneNumber\)/);
  assert.match(workspace, /id=\{`scene-row-\$\{index \+ 1\}`\}/);
  assert.doesNotMatch(workspace, /did not help/);
});
