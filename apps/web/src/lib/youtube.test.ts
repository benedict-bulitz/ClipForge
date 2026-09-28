import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import {
  formatDelta,
  formatMetric,
  formatRatio,
  localDateTimeToIso,
  performanceHeadline,
  sceneTitle,
  visibleSceneRows,
  type SceneRetention,
} from "./youtube.ts";

const panel = readFileSync(new URL("../components/youtube-panel.tsx", import.meta.url), "utf8");

const scene = (overrides: Partial<SceneRetention>): SceneRetention => ({
  index: 1, scene_id: "s1", story_role: "hook", start: 0, end: 3, duration: 3, status: "ok",
  retention_entering: 1, retention_leaving: 0.71, retention_delta: -0.29, average_retention: 0.85, ...overrides,
});

test("performance headlines follow the upload lifecycle", () => {
  assert.equal(performanceHeadline({ status: "not_uploaded" }), "Not uploaded to YouTube");
  assert.equal(performanceHeadline({ status: "private" }), "Uploaded privately — analytics will become useful after publication");
  assert.equal(performanceHeadline({ status: "waiting_for_data" }), "Waiting for YouTube analytics");
});

test("unavailable metrics are never rendered as zero", () => {
  assert.equal(formatMetric("views", { value: 5400, availability: "available", reason: null, source: "youtube_analytics_api" }), "5,400");
  assert.equal(formatMetric("engagedViews", { value: null, availability: "unavailable", reason: "rejected_by_api", source: "youtube_analytics_api" }), "unavailable");
  assert.equal(formatMetric("averageViewPercentage", { value: 74.04, availability: "available", reason: null, source: "youtube_analytics_api" }), "74.0%");
  assert.equal(formatMetric("likes", undefined), "unavailable");
});

test("scene retention rows read like the spec", () => {
  assert.equal(sceneTitle(scene({})), "Scene 1 · Hook");
  assert.equal(sceneTitle(scene({ index: 2, story_role: null })), "Scene 2");
  assert.equal(`${formatRatio(1)} → ${formatRatio(0.71)}`, "100% → 71%");
  assert.equal(formatDelta(-0.29), "Δ −29%");
  assert.equal(formatDelta(0.02), "Δ +2%");
  const rows = visibleSceneRows([scene({}), scene({ index: 2, status: "below_bucket_resolution", retention_entering: null, retention_leaving: null, retention_delta: null })]);
  assert.deepEqual(rows.map((row) => row.index), [1]);
});

test("the chosen local date and time become one absolute instant", () => {
  const iso = localDateTimeToIso("2026-10-01", "18:30");
  assert.ok(iso && iso.endsWith("Z"));
  assert.equal(new Date(iso!).getHours(), 18);
  assert.equal(localDateTimeToIso("2026-10-01", ""), null);
});

test("the panel never offers automatic publication or Stayed-to-watch", () => {
  assert.match(panel, /Upload privately/);
  assert.match(panel, /Schedule publication/);
  assert.doesNotMatch(panel, /publish now/i);
  assert.doesNotMatch(panel, /stayed to watch/i);
  assert.match(panel, /Opening retention/);
});
