import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { appliedQualityRepairs, groupQualityReport, qualityCounts, qualityReport, qualityReviewSummary, qualitySummaryParts, remainingQualityIssues, sceneListLabel, sceneQualityState, unresolvedQualityScenes } from "./quality-review.ts";
import type { FinalQualityReportEntry, FinalQualityReview } from "./types.ts";

const workspace = readFileSync(new URL("../components/project-workspace.tsx", import.meta.url), "utf8");
const css = readFileSync(new URL("../app/globals.css", import.meta.url), "utf8");

const legacy: FinalQualityReview = {
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

function entry(overrides: Partial<FinalQualityReportEntry> & Pick<FinalQualityReportEntry, "scene_id" | "scene_number" | "status" | "title">): FinalQualityReportEntry {
  const fix = overrides.status === "manual"
    ? { kind: "change_media" as const, label: "Change media", scene_id: overrides.scene_id, scene_number: overrides.scene_number, description: `Choose another visual for scene ${overrides.scene_number}` }
    : null;
  return { id: `${overrides.scene_id}:${overrides.title}`, issue_ids: [], code: "x", category: "x", severity: "error", message: "", reason: null, reason_text: null, detail: null, fix, ...overrides };
}

const BUDGET = "No real-media alternative fit, and the AI image budget is used up.";
// Shaped like the observed run: a few fixes, repeated "no relevant visual"
// scenes, framing and text-heavy leftovers without a manual control.
const report: FinalQualityReportEntry[] = [
  entry({ scene_id: "s4", scene_number: 4, status: "fixed", title: "Overlay covers too much of the visual", detail: "simplified the overlay" }),
  entry({ scene_id: "s6", scene_number: 6, status: "fixed", title: "Visual does not match the narration", detail: "reused a visual that fits this scene, with its information as an overlay" }),
  ...[14, 3, 12, 11, 13].map((number) => entry({ scene_id: `s${number}`, scene_number: number, status: "manual", title: "Visual does not match the narration", reason: "project_budget_exhausted", reason_text: BUDGET })),
  entry({ scene_id: "s2", scene_number: 2, status: "manual", title: "Subject leaves the frame", reason: "framing_not_improved", reason_text: "Automatic crop and motion changes did not improve the framing." }),
  entry({ scene_id: "s7", scene_number: 7, status: "unfixable", title: "Scene is text-heavy", reason: "informative_text_kept", reason_text: "The remaining text carries information, so it was kept." }),
  entry({ scene_id: "s9", scene_number: 9, status: "unfixable", title: "Scene is text-heavy", reason: "text_still_dominant", reason_text: "Reducing the overlay and callouts did not make the scene less text-heavy." }),
];
const reviewed: FinalQualityReview = { ...legacy, report, summary: { ...legacy.summary!, fixed_count: 2, manual_count: 6, unfixable_count: 2 } };

test("quality review summarises the final video compactly", () => {
  assert.deepEqual(qualityReviewSummary(legacy), { label: "Repaired 2 scenes · 2 issues remain", tone: "attention" });
  assert.equal(qualityReviewSummary(undefined), null); // older projects show nothing
  assert.equal(qualityReviewSummary({ version: 1, status: "disabled" }), null);
  assert.deepEqual(qualityReviewSummary({ ...legacy, revision: 3 }, 4), { label: "Repaired 2 scenes · 2 issues remain (earlier render)", tone: "muted" });
});

test("summary counts come from the actual report, never from fixed wording", () => {
  assert.deepEqual(qualityCounts(reviewed), { fixed: 2, manual: 6, unfixable: 2 });
  assert.deepEqual(qualitySummaryParts(reviewed).map((part) => part.text), ["2 fixed automatically", "6 can be fixed manually", "2 could not be safely repaired"]);
  const onlyFixed = { ...reviewed, report: report.filter((item) => item.status === "fixed") };
  assert.deepEqual(qualitySummaryParts(onlyFixed).map((part) => part.text), ["2 fixed automatically"]); // zero counts are left out
  assert.deepEqual(qualitySummaryParts({ version: 2, status: "passed", report: [] }), []);
});

test("report is grouped into fixed / needs decision / not fixable without duplicates", () => {
  const groups = groupQualityReport(reviewed);
  assert.deepEqual(groups.map((group) => [group.status, group.title]), [
    ["fixed", "Fixed automatically"], ["manual", "Needs your decision"], ["unfixable", "Could not be fixed safely"],
  ]);
  // Repeated "no relevant visual" wording collapses into one row ...
  const media = groups[1].items.find((item) => item.note === BUDGET)!;
  assert.equal(media.title, "Visual does not match the narration");
  assert.deepEqual(media.scenes.map((scene) => scene.sceneNumber), [3, 11, 12, 13, 14]);
  assert.equal(sceneListLabel(media.scenes.map((scene) => scene.sceneNumber)), "Scenes 3, 11, 12, 13 and 14");
  // ... while every scene keeps its own Fix navigation.
  assert.deepEqual(media.scenes.map((scene) => scene.fix?.scene_number), [3, 11, 12, 13, 14]);
  // Different reasons stay separate rows (no scene-specific difference hidden).
  const textHeavy = groups[2].items.filter((item) => item.title === "Scene is text-heavy");
  assert.equal(textHeavy.length, 2);
  // No issue appears twice.
  const all = groups.flatMap((group) => group.items.flatMap((item) => item.scenes.map((scene) => `${group.status}:${item.title}:${scene.sceneId}`)));
  assert.equal(new Set(all).size, all.length);
  assert.equal(all.length, report.length);
  // Fixed rows say what the repair did.
  assert.equal(groups[0].items[0].note, "Simplified the overlay");
});

test("Fix actions exist only where a manual control can help", () => {
  const groups = groupQualityReport(reviewed);
  for (const group of groups) {
    for (const item of group.items) {
      for (const scene of item.scenes) assert.equal(scene.fix !== null, group.status === "manual", `${group.status} ${item.title}`);
    }
  }
  // The workspace renders a button only for a real fix, and plain labels otherwise.
  assert.match(workspace, /scene\.fix \? \(\s*<button/);
  assert.match(workspace, /className="scene-chip">Scene \{scene\.sceneNumber\}/);
  assert.match(workspace, /onClick=\{\(\) => onFixScene\(scene\.sceneNumber\)\}/);
  // The accessible name starts with the visible label (WCAG 2.5.3) and says what the fix does.
  assert.match(workspace, /aria-label=\{`Fix scene \$\{scene\.sceneNumber\}: \$\{item\.title\}\. \$\{scene\.fix\.description\}\.`\}/);
  assert.match(workspace, /chooseSceneMedia\(sceneNumber\)/);
  assert.match(workspace, /id=\{`scene-row-\$\{index \+ 1\}`\}/);
});

test("scene markers follow the per-issue report", () => {
  assert.equal(sceneQualityState(reviewed, "s4"), "repaired");
  assert.equal(sceneQualityState(reviewed, "s3"), "issue");
  assert.equal(sceneQualityState(reviewed, "s1"), null);
});

test("older reviews without a report still show fixed and open scenes", () => {
  assert.deepEqual(appliedQualityRepairs(legacy), [
    { sceneId: "s4", sceneNumber: 4, text: "replaced a weak visual" },
    { sceneId: "s5", sceneNumber: 5, text: "disabled unsafe camera motion" },
  ]);
  assert.deepEqual(unresolvedQualityScenes(legacy).map((scene) => [scene.sceneNumber, scene.issueCount]), [[11, 2], [12, 1]]);
  assert.deepEqual(remainingQualityIssues(legacy).map((issue) => issue.scene_number), [11, 11, 12]);
  assert.deepEqual(qualityReport(legacy).map((item) => [item.status, item.scene_number]), [["fixed", 4], ["fixed", 5], ["manual", 11], ["manual", 12]]);
  assert.equal(sceneQualityState(legacy, "s4"), "repaired");
  assert.equal(sceneQualityState(legacy, "s11"), "issue");
  assert.equal(sceneQualityState(legacy, "s3"), null);
});

test("the panel shows one status display and readable, tokenised controls", () => {
  const panel = workspace.slice(workspace.indexOf("function QualityReviewPanel("), workspace.indexOf("function AIReviewPanel("));
  // Counts replace the legacy label instead of being shown next to it.
  assert.match(panel, /parts\.length > 0 && !stale\s*\?\s*parts\.map/);
  assert.equal((panel.match(/summary\.label/g) ?? []).length, 1);
  // No tiny hard-coded sizes inside the Quality Review panel.
  assert.doesNotMatch(panel, /text-\[(8|9|10|11)px\]/);
  assert.match(panel, /className="cf-action"/);
  assert.match(panel, /status-badge/);
  // The scene list uses the same semantic roles (no 8-10px labels left).
  const scenes = workspace.slice(workspace.indexOf("function ScenesView("), workspace.indexOf("function SourcesView("));
  assert.doesNotMatch(scenes, /text-\[(8|9|10)px\]/);
});

function remValue(token: string): number {
  const match = css.match(new RegExp(`${token}:\\s*([\\d.]+)rem`));
  assert.ok(match, token);
  return Number(match[1]) * 16;
}

test("semantic type and control tokens keep text and targets readable", () => {
  assert.ok(remValue("--text-label") >= 12);
  assert.ok(remValue("--text-meta") >= 13);
  assert.ok(remValue("--text-body-sm") >= 14);
  assert.ok(remValue("--text-body") >= 15 && remValue("--text-body") <= 17); // readable, not mobile-sized
  assert.ok(remValue("--control-height-xs") >= 32);
  const action = css.slice(css.indexOf(".cf-action {"), css.indexOf("}", css.indexOf(".cf-action {")));
  assert.match(action, /min-height: var\(--control-height-xs\)/);
  assert.match(action, /font-size: var\(--text-body-sm\)/);
});

test("status colours come from theme tokens that both themes define", () => {
  for (const rule of [".status-fixed", ".status-manual", ".status-unfixable", ".status-muted", ".quality-review", ".cf-action", ".scene-chip"]) {
    const start = css.indexOf(`${rule} {`);
    assert.ok(start >= 0, rule);
    const body = css.slice(start, css.indexOf("}", start));
    assert.doesNotMatch(body, /#[0-9a-f]{3,6}\b|rgba?\(/i, `${rule} uses a hard-coded colour`);
  }
  const dark = css.slice(css.indexOf("html.dark {"), css.indexOf("}", css.indexOf("html.dark {")));
  for (const token of ["--success", "--warning", "--destructive", "--surface-elevated", "--muted-foreground", "--border"]) assert.match(dark, new RegExp(`${token}:`));
});
