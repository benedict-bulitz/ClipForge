import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import {
  attentionSummary,
  deleteDialogCopy,
  formatBytes,
  formatDelta,
  formatLocalDate,
  formatMetric,
  formatRatio,
  formatScheduleConfirmation,
  parseTags,
  performanceHeadline,
  primaryActionLabel,
  regionFromLocale,
  sceneTitle,
  statusRows,
  tagsLength,
  uses24HourClock,
  visibleSceneRows,
  zoneLabel,
  type DeletionPlan,
  type SceneRetention,
  type YouTubeUpload,
} from "./youtube.ts";

const sheet = readFileSync(new URL("../components/youtube-publish-sheet.tsx", import.meta.url), "utf8");
const panel = readFileSync(new URL("../components/youtube-panel.tsx", import.meta.url), "utf8");
const workspace = readFileSync(new URL("../components/project-workspace.tsx", import.meta.url), "utf8");
const defaults = readFileSync(new URL("../components/youtube-upload-defaults.tsx", import.meta.url), "utf8");

const scene = (overrides: Partial<SceneRetention>): SceneRetention => ({
  index: 1, scene_id: "s1", story_role: "hook", start: 0, end: 3, duration: 3, status: "ok",
  retention_entering: 1, retention_leaving: 0.71, retention_delta: -0.29, average_retention: 0.85, ...overrides,
});

const upload = (overrides: Partial<YouTubeUpload> = {}): YouTubeUpload => ({
  id: "u1", project_id: "p", project_revision: 2, render_revision: 1, render_sha256: "x", channel_id: "UC",
  youtube_video_id: "vid1", state: "processing", lifecycle: "scheduled", privacy_status: "private",
  publish_at: "2026-09-28T18:30:00Z", schedule_status: "scheduled", schedule_error: null, published_at: null,
  upload_status: "uploaded", processing_status: null, failure_reason: null, rejection_reason: null, content_type: null,
  deleted_on_youtube: false, title: "t", video_status: "processing",
  thumbnail: { status: "failed", source: "generated", asset: "youtube-cover-3", failure_reason: "not allowed", applied_at: null },
  audience: { made_for_kids: false, confirmed_by_youtube: false, answered: true },
  contains_synthetic_media: false, requested_visibility: "schedule", visibility_restricted: false,
  schedule: { status: "scheduled", publish_at: "2026-09-28T18:30:00Z", local_time: "2026-09-28T20:30", timezone: "Europe/Berlin", error: null },
  source_kind: "render", progress: null, error: null, is_active_mapping: true, can_reupload: false, uploaded_at: null,
  last_analytics_sync_at: null, watch_url: "w", shorts_url: "s", studio_url: "st", ...overrides,
});

test("performance headlines follow the upload lifecycle", () => {
  assert.equal(performanceHeadline({ status: "not_uploaded" }), "Not uploaded to YouTube");
  assert.equal(performanceHeadline({ status: "private" }), "Uploaded privately — analytics will become useful after publication");
  assert.equal(performanceHeadline({ status: "waiting_for_data" }), "Waiting for YouTube analytics");
});

test("unavailable metrics are never rendered as zero", () => {
  assert.equal(formatMetric("views", { value: 5400, availability: "available", reason: null, source: "youtube_analytics_api" }), "5,400");
  assert.equal(formatMetric("engagedViews", { value: null, availability: "unavailable", reason: "rejected_by_api", source: "youtube_analytics_api" }), "unavailable");
  assert.equal(formatMetric("likes", undefined), "unavailable");
});

test("scene retention rows read like the spec", () => {
  assert.equal(sceneTitle(scene({})), "Scene 1 · Hook");
  assert.equal(`${formatRatio(1)} → ${formatRatio(0.71)}`, "100% → 71%");
  assert.equal(formatDelta(-0.29), "Δ −29%");
  assert.deepEqual(visibleSceneRows([scene({}), scene({ index: 2, status: "below_bucket_resolution", retention_entering: null, retention_leaving: null })]).map((row) => row.index), [1]);
});

test("time zone, 24-hour clock and DST-aware confirmation", () => {
  assert.equal(uses24HourClock("de-DE"), true);
  assert.equal(uses24HourClock("en-US"), false);
  assert.equal(formatLocalDate("2026-09-28", "de-DE"), "28.09.2026");
  assert.equal(regionFromLocale("de-DE"), "DE");
  assert.equal(regionFromLocale("en"), "US");
  assert.equal(zoneLabel("Europe/Berlin", { abbreviation: "CEST", utc_offset: "UTC+02:00" }), "Europe/Berlin (CEST · UTC+02:00)");
  assert.match(zoneLabel("Europe/Berlin"), /^Europe\/Berlin \(UTC\+0[12]:00\)$/);
  const summer = formatScheduleConfirmation("2026-09-28T18:30:00Z", "Europe/Berlin", "en-GB");
  assert.match(summer, /Monday/);
  assert.match(summer, /28 September 2026/);
  assert.match(summer, /20:30/);
  // Winter time: the same UTC wall clock maps one hour earlier in Berlin.
  assert.match(formatScheduleConfirmation("2026-12-01T19:30:00Z", "Europe/Berlin", "en-GB"), /20:30/);
  assert.match(formatScheduleConfirmation("2026-09-28T18:30:00Z", "Europe/Berlin", "de-DE"), /Montag.*28\. September 2026.*20:30/);
  assert.match(formatScheduleConfirmation("2026-09-28T18:30:00Z", "Europe/Berlin", "en-US"), /8:30\sPM/);
});

test("preflight and CTA copy", () => {
  assert.equal(primaryActionLabel("private"), "Upload privately");
  assert.equal(primaryActionLabel("schedule"), "Upload and schedule");
  assert.equal(attentionSummary([{ field: "made_for_kids", message: "Choose whether this video is made for kids" }, { field: "thumbnail", message: "Select a thumbnail" }]), "2 items need attention:");
  assert.equal(attentionSummary([]), null);
  assert.deepEqual(parseTags("#aviation, windows , ,a b"), ["aviation", "windows", "a b"]);
  assert.equal(tagsLength(["a b", "c"]), 7);
});

test("status is shown as independent rows, never one vague state", () => {
  const rows = statusRows(upload(), "en-GB");
  assert.deepEqual(rows.map((row) => row.label), ["Video", "Thumbnail", "Audience", "Visibility", "Schedule"]);
  assert.equal(rows[0].value, "Processing");
  assert.equal(rows[1].value, "Failed");
  assert.equal(rows[2].value, "Not made for kids");
  assert.equal(rows[3].value, "Scheduled");
  assert.match(rows[4].value, /20:30 · Europe\/Berlin/);
  assert.equal(statusRows(upload({ audience: { made_for_kids: null, confirmed_by_youtube: null, answered: false } }), "en-GB")[2].tone, "warn");
});

test("delete dialog copy follows the upload lifecycle", () => {
  const plan = (mode: DeletionPlan["mode"]): DeletionPlan => ({ project_id: "p", title: "t", mode, reclaimable_bytes: 1_503_238_553, retained_bytes: 2_202_009, verification: "live", uploads: [], messages: [] });
  assert.equal(deleteDialogCopy(plan("full")).title, "Delete project permanently?");
  assert.match(deleteDialogCopy(plan("full")).body, /never uploaded to YouTube/);
  assert.equal(deleteDialogCopy(plan("archive")).confirm, "Delete local project");
  assert.match(deleteDialogCopy(plan("archive")).body, /keep the compact performance\/analytics record/);
  assert.equal(deleteDialogCopy(plan("unverified")).title, "Upload status could not be verified.");
  assert.equal(formatBytes(1_503_238_553), "1.4 GB");
  assert.equal(formatBytes(2_202_009), "2.1 MB");
  assert.equal(formatBytes(null), "unknown");
});

test("publishing sheet asks every required question explicitly and fakes nothing", () => {
  assert.match(sheet, /aria-label="Made for kids" aria-required="true"/);
  assert.match(sheet, /No, it's not made for kids/);
  assert.match(sheet, /Yes, it's made for kids/);
  assert.doesNotMatch(sheet, /type="checkbox"[^>]*made_for_kids/);
  assert.match(sheet, /Realistic altered or synthetic content\?/);
  assert.match(sheet, /Suggested:/);
  assert.match(sheet, /Additional YouTube Studio settings/);
  assert.match(sheet, /primaryActionLabel\(options\.visibility\)/);
  assert.match(sheet, /disabled=\{blocked\}/);
  assert.doesNotMatch(sheet, /stayed to watch|publish now|Automatic chapters.*checkbox/i);
  for (const control of ["Allow embedding", "Show public statistics", "Notify subscribers", "Contains paid promotion", "Recording date", "Category", "License"]) {
    assert.match(sheet, new RegExp(control));
  }
  assert.match(panel, /Retry thumbnail/);
  assert.match(panel, /Open in Studio/);
  assert.match(panel, /Change schedule/);
  assert.match(panel, /Thumbnail could not be applied/);
  assert.match(defaults, /Ask me for every video/);
});

test("Upload to YouTube is the primary action and export is secondary", () => {
  assert.match(workspace, /variant="accent"[^\n]*setPublishOpen\(true\)/);
  assert.match(workspace, /Upload to YouTube/);
  assert.match(workspace, /Save local MP4/);
  assert.doesNotMatch(workspace, /"Export MP4"/);
});
