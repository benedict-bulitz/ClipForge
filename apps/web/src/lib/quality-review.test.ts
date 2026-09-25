import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { appliedQualityRepairs, qualityReviewSummary, remainingQualityIssues, sceneQualityState } from "./quality-review.ts";
import type { FinalQualityReview } from "./types.ts";

const workspace = readFileSync(new URL("../components/project-workspace.tsx", import.meta.url), "utf8");

const review: FinalQualityReview = {
  version: 1,
  status: "issues_remain",
  summary: { label: "Repaired 1 scene · 1 issue remains", repaired_scene_count: 1, remaining_issue_count: 1, warning_count: 1 },
  issues: [
    { id: "b", scene_id: "scene_03_01", scene_number: 4, category: "overlay_quality", code: "overlay_large", severity: "warning", message: "Large overlay." },
    { id: "a", scene_id: "scene_01_01", scene_number: 1, category: "semantic_match", code: "wrong_media", severity: "error", message: "Wrong visual." },
  ],
  repairs: [
    { scene_id: "scene_02_01", scene_number: 2, action: "adjust_composition", status: "applied", outcome: "resolved", adjustments: { overlay: { mode: "compact" } } },
    { scene_id: "scene_01_01", scene_number: 1, action: "replace_media", status: "blocked", blocked_reason: "user_locked_visual" },
  ],
  changed_scenes: ["scene_02_01"],
};

test("quality review summarises the final video compactly", () => {
  assert.deepEqual(qualityReviewSummary(review), { label: "Repaired 1 scene · 1 issue remains", tone: "attention" });
  assert.deepEqual(qualityReviewSummary({ version: 1, status: "passed", summary: { label: "Passed", repaired_scene_count: 0, remaining_issue_count: 0, warning_count: 0 } }), { label: "Passed", tone: "passed" });
  assert.equal(qualityReviewSummary(undefined), null); // older projects show nothing
  assert.deepEqual(qualityReviewSummary({ ...review, revision: 3 }, 4), { label: "Repaired 1 scene · 1 issue remains (earlier render)", tone: "muted" });
  assert.equal(qualityReviewSummary({ ...review, revision: 4 }, 4)?.tone, "attention");
  assert.equal(qualityReviewSummary({ version: 1, status: "disabled" }), null);
});

test("remaining issues point at scenes, errors first", () => {
  assert.deepEqual(remainingQualityIssues(review).map((issue) => issue.scene_number), [1, 4]);
});

test("automatic repairs are visible to the user", () => {
  assert.deepEqual(appliedQualityRepairs(review), [{ sceneId: "scene_02_01", sceneNumber: 2, text: "simplified the overlay", outcome: "resolved" }]);
  assert.equal(sceneQualityState(review, "scene_02_01"), "repaired");
  assert.equal(sceneQualityState(review, "scene_01_01"), "issue");
  assert.equal(sceneQualityState(review, "scene_09_01"), null);
});

test("workspace links unresolved issues to the existing Change Media flow", () => {
  assert.match(workspace, /<QualityReviewPanel/);
  assert.match(workspace, /onFixScene=\{/);
  assert.match(workspace, /chooseSceneMedia\(sceneNumber\)/);
});
