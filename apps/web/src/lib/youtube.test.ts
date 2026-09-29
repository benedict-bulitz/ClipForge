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
  publishPhase,
  SUCCESS_VISIBLE_MS,
  FINISH_GRACE_MS,
  regionFromLocale,
  sceneTitle,
  statusRows,
  tagsLength,
  uses24HourClock,
  visibleSceneRows,
  zoneLabel,
  analyticsReadiness,
  currentHeadline,
  freshnessLine,
  scheduleLine,
  type CurrentStatus,
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

const current = (overrides: Partial<CurrentStatus>): CurrentStatus => ({
  state: "scheduled", label: "Scheduled", processing: false, stale: false, stale_reason: null,
  last_checked_at: "2026-09-28T18:00:00Z", last_attempt_at: "2026-09-28T18:00:00Z", refresh_error: null,
  scheduled_for: "2026-09-28T18:30:00Z", published_at: null, published_time_source: null, first_observed_public_at: null,
  remote: { privacy_status: "private", upload_status: "processed", processing_status: null, publish_at: "2026-09-28T18:30:00Z", published_at: null, rejection_reason: null, failure_reason: null },
  live_stats: null,
  requested: { visibility: "schedule", publish_at: "2026-09-28T18:30:00Z", local_time: "2026-09-28T20:30", timezone: "Europe/Berlin", history: [] },
  ...overrides,
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
  last_analytics_sync_at: null, watch_url: "w", shorts_url: "s", studio_url: "st", next_status_check_in_seconds: null,
  current: current({}), ...overrides,
});

test("performance headlines follow YouTube's confirmed state", () => {
  assert.equal(performanceHeadline({ status: "not_uploaded" }), "Not uploaded to YouTube");
  assert.equal(performanceHeadline({ status: "private", analytics_state: "not_published" }), "Not published — detailed analytics start after publication");
  assert.equal(performanceHeadline({ status: "waiting_for_data", analytics_state: "processing" }), "Detailed analytics · Processing on YouTube");
});

test("real bug: once YouTube reports public the UI says Published with live stats, not Scheduled", () => {
  const now = new Date("2026-09-28T18:47:00Z");
  const published = current({
    state: "published", label: "Published", scheduled_for: null, published_at: "2026-09-28T18:30:00Z",
    published_time_source: "youtube_snippet_published_at", last_checked_at: "2026-09-28T18:47:00Z",
    remote: { privacy_status: "public", upload_status: "processed", processing_status: null, publish_at: null, published_at: "2026-09-28T18:30:00Z", rejection_reason: null, failure_reason: null },
    live_stats: { views: 12, likes: 1, comments: 0, checked_at: "2026-09-28T18:47:00Z", source: "youtube_data_api_videos_list" },
  });
  assert.equal(currentHeadline(published, now, "en-GB"), "Published · 17 minutes ago");
  assert.equal(freshnessLine(published, now, "de-DE", "Europe/Berlin"), "Last checked 20:47");
  const rows = statusRows(upload({ current: published, lifecycle: "published" }), "en-GB");
  assert.equal(rows.find((row) => row.label === "Visibility")?.value, "Public");
  assert.match(rows.find((row) => row.label === "Schedule")?.value ?? "", /^Published 28\/09\/2026 · 20:30 · Europe\/Berlin$/);
  assert.doesNotMatch(JSON.stringify(rows), /Scheduled/);
});

test("scheduled, stale and pending states read accurately", () => {
  const now = new Date("2026-09-28T18:10:00Z");
  assert.equal(scheduleLine("2026-09-28T18:30:00Z", "Europe/Berlin", "de-DE"), "28.09.2026 · 20:30");
  const stale = current({ stale: true, stale_reason: "refresh_failed", last_checked_at: "2026-09-28T18:02:00Z", refresh_error: { code: "network_timeout", message: "timed out" } });
  assert.equal(currentHeadline(stale, now, "en-GB"), "Scheduled");
  assert.equal(freshnessLine(stale, now, "en-GB"), "Last confirmed 8 minutes ago · Could not refresh YouTube status");
  assert.equal(currentHeadline(current({ state: "publish_pending", label: "Still private after the scheduled time" }), now, "en-GB"), "Still private after the scheduled time");
});

test("analytics readiness copy makes no timing promise", () => {
  const processing = analyticsReadiness("processing");
  assert.equal(processing?.title, "Processing on YouTube");
  assert.match(processing?.body ?? "", /may take time/);
  assert.doesNotMatch(JSON.stringify([processing, analyticsReadiness("partial")]), /\d+\s*(–|-|to)?\s*\d*\s*hours?/i);
  assert.equal(analyticsReadiness("available"), null);
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
  assert.equal(rows[0].value, "Processing");  // video_status
  assert.equal(rows[1].value, "Failed");
  assert.equal(rows[2].value, "Not made for kids");
  assert.equal(rows[3].value, "Scheduled");
  assert.match(rows[4].value, /28\/09\/2026 · 20:30 · Europe\/Berlin/);
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
  assert.match(panel, /Refresh YouTube status/);
  assert.match(panel, /Live stats/);
  assert.match(panel, /next_status_check_in_seconds/);
  assert.match(panel, /MAX_STATUS_POLLS/);
  assert.doesNotMatch(panel, /stays private until then/);
  assert.match(panel, /Thumbnail could not be applied/);
  assert.match(defaults, /Ask me for every video/);
});

test("Upload to YouTube is the primary action and export is secondary", () => {
  assert.match(workspace, /variant="accent"[^\n]*setPublishOpen\(true\)/);
  assert.match(workspace, /Upload to YouTube/);
  assert.match(workspace, /Save local MP4/);
  assert.doesNotMatch(workspace, /"Export MP4"/);
});

// ---------------------------------------------------------------------------
// Publishing sheet: upload -> scheduling -> success feedback
// ---------------------------------------------------------------------------

const T0 = Date.parse("2026-09-28T18:00:00Z");
const fresh = (overrides: Partial<YouTubeUpload> = {}) => upload({ thumbnail: { status: "applied", source: "generated", asset: "youtube-cover-3", failure_reason: null, applied_at: "x" }, ...overrides });
const uploading = fresh({ state: "uploading", lifecycle: "uploading", youtube_video_id: null, progress: 0.42, schedule_status: "none", current: current({ state: "uploading", last_checked_at: null, last_attempt_at: null }) });

test("while the video uploads the button says Uploading… with progress and never closes", () => {
  const phase = publishPhase(uploading, true, null, T0);
  assert.deepEqual([phase.phase, phase.label, phase.tone, phase.close], ["uploading", "Uploading… 42%", "busy", false]);
});

test("after the bytes land a scheduled upload shows Scheduling… until YouTube answers", () => {
  const waiting = fresh({ thumbnail: { ...fresh().thumbnail, status: "pending" }, current: current({ last_checked_at: null, last_attempt_at: null }) });
  assert.equal(publishPhase(waiting, true, T0, T0 + 1000).label, "Scheduling…");
  assert.equal(publishPhase(waiting, false, T0, T0 + 1000).label, "Finishing…");
});

test("success is green Scheduled ✓ / Uploaded ✓ and lets the sheet close after ~1.4 s", () => {
  const scheduled = publishPhase(fresh(), true, T0, T0);
  assert.deepEqual([scheduled.phase, scheduled.label, scheduled.tone, scheduled.close], ["success", "Scheduled ✓", "success", true]);
  const privateUpload = fresh({ requested_visibility: "private", schedule_status: "none", current: current({ state: "private", label: "Private", remote: { ...current({}).remote, publish_at: null } }) });
  assert.deepEqual([publishPhase(privateUpload, false, T0, T0).label, publishPhase(privateUpload, false, T0, T0).tone], ["Uploaded ✓", "success"]);
  assert.ok(SUCCESS_VISIBLE_MS >= 1000 && SUCCESS_VISIBLE_MS <= 1500);
});

test("a failed upload keeps the sheet open with Retry upload", () => {
  const failed = fresh({ state: "failed", lifecycle: "failed", youtube_video_id: null, error: { code: "quota_exceeded", message: "Quota exceeded." }, current: current({ state: "upload_failed", last_checked_at: null }) });
  const phase = publishPhase(failed, true, null, T0);
  assert.deepEqual([phase.phase, phase.label, phase.retry, phase.close], ["error", "Retry upload", "upload", false]);
  assert.equal(phase.message, "Upload failed. Quota exceeded.");
  const unknown = publishPhase({ ...failed, error: { code: "session_expired_unknown_outcome", message: "Check YouTube Studio." } }, false, null, T0);
  assert.equal(unknown.retry, null); // never risk a duplicate video
});

test("a schedule failure is never shown as Scheduled ✓", () => {
  const failed = publishPhase(fresh({ schedule_status: "schedule_failed", schedule_error: "Invalid publish time." }), true, T0, T0);
  assert.deepEqual([failed.phase, failed.label, failed.retry, failed.close], ["partial", "Retry scheduling", "schedule", false]);
  assert.equal(failed.message, "Video uploaded, but scheduling failed. Invalid publish time.");
  const dropped = publishPhase(fresh({ current: current({ state: "private", label: "Private", remote: { ...current({}).remote, publish_at: null } }) }), true, T0, T0);
  assert.equal(dropped.label, "Retry scheduling");
  const unconfirmed = publishPhase(fresh({ current: current({ last_checked_at: null }) }), true, T0, T0 + FINISH_GRACE_MS + 1);
  assert.notEqual(unconfirmed.label, "Scheduled ✓");
  assert.equal(unconfirmed.tone, "warn");
});

test("a thumbnail failure is a partial success, not a total failure", () => {
  const partial = publishPhase(upload(), false, T0, T0);
  assert.deepEqual([partial.phase, partial.label, partial.tone, partial.close], ["partial", "Uploaded · thumbnail needs attention", "warn", true]);
  assert.match(partial.message ?? "", /Video uploaded successfully/);
  assert.equal(publishPhase(upload(), true, T0, T0).label, "Scheduled · thumbnail needs attention");
});

test("the sheet stays open while uploading and closes itself only after success", () => {
  assert.doesNotMatch(sheet, /uploadProjectToYouTube\([^)]*\);\s*onUploaded\(\)/);
  assert.match(sheet, /follow\(result\.upload, wantsSchedule\)/);
  assert.match(sheet, /phase\?\.close \? \(phase\.tone === "success" \? SUCCESS_VISIBLE_MS : PARTIAL_VISIBLE_MS\)/);
  assert.match(sheet, /window\.setTimeout\(\(\) => done\.current\(\), closeAfter\)/);
  // no double submit: the handler bails while in flight and the button is disabled
  assert.match(sheet, /if \(!options \|\| blocked \|\| locked\) return;/);
  assert.match(sheet, /const blocked = issues\.length > 0 \|\| checking \|\| inFlight \|\| unresolvedConflict;/);
  assert.match(sheet, /aria-busy=\{phase\.tone === "busy"\}/);
  // green check success, announced to assistive technology
  assert.match(sheet, /success: "!bg-emerald-600/);
  assert.match(sheet, /<CheckCircle2/);
  assert.match(sheet, /role="status" aria-live="polite"/);
  assert.match(sheet, /Video uploaded, but scheduling failed/);
  assert.match(sheet, /scheduleYouTubeUpload\(run\.id, options\.schedule\)/);
  assert.match(sheet, /retryYouTubeUpload\(run\.id\)/);
  assert.doesNotMatch(panel, /Upload started\. You can keep working/);
});

test("the sheet uses the suggested schedule and says where pre-selected settings came from", () => {
  assert.match(sheet, /schedule: next\.options\.schedule \?\? \{ date: "", time: "", timezone \}/);
  assert.match(sheet, /preset_source === "last_upload"/);
  assert.match(sheet, /Title, description, tags and thumbnail come from this project\./);
});
