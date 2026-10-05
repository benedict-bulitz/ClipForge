import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import type { Timers } from "./generation-poll.ts";
import { createHomePoller, type PageVisibility } from "./home-poll.ts";
import {
  createExclusivePlayback,
  mergeLiveJobs,
  overviewRefreshNeeded,
  playableSource,
  queueProgressPercent,
  queueQualityBadge,
  queueRowStatus,
  queueYouTubeAction,
  uploadInFlight,
  type QueueItem,
  type QueueOverview,
  type QueueProjectSummary,
} from "./queue-page.ts";
import type { GenerationJob } from "./types.ts";
import type { YouTubeUpload } from "./youtube.ts";

const component = readFileSync(new URL("../components/queue-overview.tsx", import.meta.url), "utf8");
const route = readFileSync(new URL("../app/queue/page.tsx", import.meta.url), "utf8");
const home = readFileSync(new URL("../app/page.tsx", import.meta.url), "utf8");
const panel = readFileSync(new URL("../components/youtube-panel.tsx", import.meta.url), "utf8");
const api = readFileSync(new URL("./api.ts", import.meta.url), "utf8");

function job(id: string, status: GenerationJob["status"], extra: Partial<GenerationJob> = {}): GenerationJob {
  return { id: `job-${id}`, project_id: id, prompt: `Question ${id}?`, status, queue_position: null, progress: 0, base_revision: null, current_stage: "preparing", stage_label: "Preparing", completed_units: null, total_units: null, started_at: null, updated_at: "2026-10-01T09:00:00Z", completed_at: null, elapsed_seconds: 0, estimated_remaining_seconds: null, failure_category: null, failure_message: null, ...extra };
}

function project(id: string, extra: Partial<QueueProjectSummary> = {}): QueueProjectSummary {
  return {
    id,
    title: `Question ${id}?`,
    status: "rendered",
    current_revision: 2,
    render: { state: "ready", status: "complete", revision: 2, final_video_url: `/api/projects/${id}/final-video?revision=2` },
    duration_seconds: 41.6,
    width: 1080,
    height: 1920,
    poster_url: `/media/${id}/thumbnails/youtube.jpg`,
    quality: { ai_review: { status: "passed" }, final_review: { status: "passed", revision: 2, summary: { label: "Passed" } } },
    youtube: { current_render: { uploadable: true, code: null, message: null, render_revision: 2, existing_upload_id: null }, upload: null },
    ...extra,
  };
}

function upload(state: YouTubeUpload["current"]["state"], extra: Partial<YouTubeUpload> = {}): YouTubeUpload {
  return {
    id: "up-1", project_id: "A", render_revision: 2, youtube_video_id: state === "uploading" ? null : "yt123", lifecycle: state === "scheduled" ? "scheduled" : "published", progress: null,
    current: { state, label: state === "scheduled" ? "Scheduled" : state === "uploading" ? "Uploading" : "Published", scheduled_for: state === "scheduled" ? "2026-10-07T16:00:00Z" : null, requested: { timezone: "Europe/Berlin" } },
    ...extra,
  } as unknown as YouTubeUpload;
}

const rendered: QueueItem = { job: job("A", "completed", { progress: 1 }), project: project("A") };

// ---------------------------------------------------------------------------
// Queue data
// ---------------------------------------------------------------------------

test("every queue state maps to one truthful row status", () => {
  const rows: Array<[QueueItem, string, string]> = [
    [{ job: job("Q", "queued", { queue_position: 2 }), project: null }, "queued", "Queued"],
    [{ job: job("R", "running", { progress: 0.37, stage_label: "Writing script" }), project: null }, "running", "Generating"],
    [{ job: job("X", "cancelling"), project: null }, "cancelling", "Cancelling…"],
    [rendered, "rendered", "Rendered"],
    [{ job: job("F", "failed", { failure_message: "Research failed" }), project: null }, "failed", "Failed"],
    [{ job: job("C", "cancelled"), project: project("C", { render: { state: "not_rendered", status: "planned", revision: null, final_video_url: null } }) }, "cancelled", "Cancelled"],
    [{ job: job("D", "completed"), project: null }, "unavailable", "Project unavailable"],
  ];
  for (const [item, state, label] of rows) {
    const status = queueRowStatus(item);
    assert.equal(status.state, state);
    assert.equal(status.label, label);
  }
  assert.equal(queueRowStatus(rows[0][0]).detail, "Position 2 in the queue");
  assert.equal(queueRowStatus(rows[1][0]).detail, "Writing script");
  assert.equal(queueRowStatus(rows[4][0]).detail, "Research failed");
});

test("progress is only the backend's progress of a generating job - never invented", () => {
  assert.equal(queueProgressPercent(job("R", "running", { progress: 0.374 })), 37);
  assert.equal(queueProgressPercent(job("Q", "queued", { progress: 0 })), null);
  assert.equal(queueProgressPercent(job("A", "completed", { progress: 1 })), null);
  assert.equal(queueProgressPercent(job("F", "failed", { progress: 0.6 })), null);
});

test("live job progress overlays the overview rows and removed jobs drop out", () => {
  const items: QueueItem[] = [rendered, { job: job("B", "running", { progress: 0.2 }), project: null }, { job: job("C", "queued", { queue_position: 1 }), project: null }];
  const merged = mergeLiveJobs(items, [job("A", "completed"), job("B", "running", { progress: 0.8 }), job("C", "removed")]);
  assert.deepEqual(merged.map((item) => [item.job.project_id, item.job.status, item.job.progress]), [["A", "completed", 0], ["B", "running", 0.8]]);
  assert.equal(mergeLiveJobs(items, null), items);
});

// ---------------------------------------------------------------------------
// Preview
// ---------------------------------------------------------------------------

test("only a completed job's current, complete render is playable - and it is the final MP4 URL", () => {
  assert.equal(playableSource(rendered), "/api/projects/A/final-video?revision=2");
  for (const state of ["missing", "stale", "not_rendered"] as const) {
    assert.equal(playableSource({ job: job("A", "completed"), project: project("A", { render: { state, status: "complete", revision: 2, final_video_url: null } }) }), null);
  }
  assert.equal(playableSource({ job: job("A", "running"), project: project("A") }), null);
  assert.equal(playableSource({ job: job("A", "completed"), project: null }), null);
  assert.equal(queueRowStatus({ job: job("A", "completed"), project: project("A", { render: { state: "stale", status: "complete", revision: 1, final_video_url: null } }) }).label, "Render outdated");
  assert.equal(queueRowStatus({ job: job("A", "completed"), project: project("A", { render: { state: "missing", status: "complete", revision: 2, final_video_url: null } }) }).label, "Render missing");
});

test("the preview is a poster first; a click mounts the final video inline with no autoplay", () => {
  // No <video> exists before the click: the poster is a button, the player mounts on activation.
  assert.match(component, /if \(active && videoSource && !failed\)/);
  assert.match(component, /<img src=\{posterSource\}[^>]*loading="lazy"/);
  assert.match(component, /flushSync\(\(\) => setActive\(true\)\)/);
  assert.match(component, /element\.play\(\)/);
  assert.match(component, /preload="metadata"/);
  assert.match(component, /aspect-\[9\/16\]/);
  assert.match(component, /aria-label=\{failed \? `Retry playing \$\{title\}` : `Play \$\{title\}`\}/);
  assert.doesNotMatch(component, /\bautoPlay\b|autoplay=/); // the JSX attribute (the docs say "never autoplay")
  const player = component.match(/<video[\s\S]*?\/>/)?.[0] ?? "";
  assert.match(player, /controls/);
  assert.doesNotMatch(player, /\bmuted\b|\bloop\b/); // the user's click starts it with sound
  assert.doesNotMatch(component, />\s*Preview\s*</); // no separate Preview button
  assert.doesNotMatch(component, /role="dialog"|window\.open|router\.push/); // no modal, window or navigation
  // Source: the row's canonical final-video URL only; no queue-specific render or copy.
  assert.match(component, /const source = playableSource\(item\)/);
  assert.match(component, /const videoSource = mediaUrl\(source\)/);
  assert.doesNotMatch(component, /render\.url|renderProject|exportProject/);
});

test("one inline player at a time: starting one pauses every other", () => {
  const playback = createExclusivePlayback();
  const paused: string[] = [];
  const element = (id: string) => ({ pause: () => paused.push(id) });
  const releaseA = playback.register("A", element("A"));
  playback.register("B", element("B"));
  playback.register("C", element("C"));
  playback.playing("B");
  assert.deepEqual(paused.sort(), ["A", "C"]);
  paused.length = 0;
  releaseA();
  playback.playing("C");
  assert.deepEqual(paused, ["B"]);
  assert.equal(playback.size, 2);
  paused.length = 0;
  playback.pauseAll();
  assert.deepEqual(paused.sort(), ["B", "C"]);
  // Wired: a click and the video's own play event both claim exclusivity; unmount pauses and releases.
  assert.match(component, /onPlay=\{\(\) => playback\.playing\(id\)\}/);
  assert.match(component, /playback\.register\(id, element\)/);
  assert.match(component, /element\.pause\(\);\s*unregister\(\);/);
});

test("a player error is contained in its row and refreshes the overview", () => {
  assert.match(component, /onError=\{\(\) => \{ setFailed\(true\); setActive\(false\); onError\(\); \}\}/);
  assert.match(component, /Video could not be loaded/);
  assert.match(component, /onError=\{\(\) => setPosterBroken\(true\)\}/);
});

// ---------------------------------------------------------------------------
// Quality badge
// ---------------------------------------------------------------------------

test("quality badge reflects the stored reviews only", () => {
  assert.equal(queueQualityBadge(project("A"))?.label, "Ready");
  assert.equal(queueQualityBadge(project("A", { quality: { ai_review: { status: "passed" }, final_review: { status: "issues_remain", revision: 2, summary: { label: "2 issues remain" } } } }))?.label, "Needs fix");
  assert.equal(queueQualityBadge(project("A", { quality: { ai_review: { status: "needs_fix" }, final_review: { status: "passed", revision: 2, summary: null } } }))?.label, "Needs fix");
  assert.equal(queueQualityBadge(project("A", { quality: { ai_review: null, final_review: { status: "not_reviewed", revision: null, summary: null } } }))?.label, "Review unavailable");
  assert.equal(queueQualityBadge(project("A", { quality: { ai_review: null, final_review: { status: "passed", revision: 1, summary: { label: "Passed" } } } }))?.label, "Review outdated");
  assert.equal(queueQualityBadge(project("A", { quality: { ai_review: { status: "passed_with_warnings" }, final_review: null } }))?.label, "Ready");
  assert.equal(queueQualityBadge(project("A", { quality: { ai_review: { status: "unavailable" }, final_review: null } }))?.label, "Review unavailable");
  assert.equal(queueQualityBadge(project("A", { render: { state: "stale", status: "complete", revision: 1, final_video_url: null } })), null);
  assert.equal(queueQualityBadge(null), null);
});

// ---------------------------------------------------------------------------
// YouTube
// ---------------------------------------------------------------------------

test("Upload to YouTube appears only when the Results page would offer it", () => {
  assert.deepEqual(queueYouTubeAction("connected", rendered), { kind: "upload", label: "Upload to YouTube" });
  assert.deepEqual(queueYouTubeAction("not_connected", rendered), { kind: "connect", label: "Connect YouTube" });
  assert.deepEqual(queueYouTubeAction("auth_expired", rendered), { kind: "connect", label: "Reconnect YouTube" });
  const uploaded: QueueItem = { job: rendered.job, project: project("A", { youtube: { current_render: { uploadable: false, code: "already_uploaded", message: "Already uploaded as yt123", render_revision: 2, existing_upload_id: "up-1" }, upload: upload("scheduled") } }) };
  assert.deepEqual(queueYouTubeAction("connected", uploaded), { kind: "none", reason: null });
  const blocked: QueueItem = { job: rendered.job, project: project("A", { youtube: { current_render: { uploadable: false, code: "not_rendered", message: "Render this revision before uploading it to YouTube." }, upload: null } }) };
  assert.deepEqual(queueYouTubeAction("connected", blocked), { kind: "none", reason: "Render this revision before uploading it to YouTube." });
  const newRevision: QueueItem = { job: rendered.job, project: project("A", { youtube: { current_render: { uploadable: true, code: null, message: null, render_revision: 3 }, upload: upload("published", { render_revision: 2 }) } }) };
  assert.deepEqual(queueYouTubeAction("connected", newRevision), { kind: "upload", label: "Upload this revision" });
  assert.deepEqual(queueYouTubeAction("connected", { job: job("B", "running"), project: null }), { kind: "none", reason: null });
  assert.deepEqual(queueYouTubeAction("connected", { job: rendered.job, project: project("A", { render: { state: "missing", status: "complete", revision: 2, final_video_url: null } }) }), { kind: "none", reason: null });
});

test("Upload to YouTube opens the canonical publishing sheet and never uploads by itself", () => {
  assert.match(component, /import \{ PublishSheet \} from "\.\/youtube-publish-sheet"/);
  assert.match(component, /<PublishSheet project=\{publishing\} onClose=\{closePublishing\} onUploaded=\{closePublishing\} \/>/);
  assert.match(component, /setPublishing\(await getProject\(projectId\)\)/);
  assert.doesNotMatch(component, /uploadProjectToYouTube|preflightYouTubeUpload|scheduleYouTubeUpload|getPublishingDraft/);
  // The Results page and the queue share the one upload decision.
  assert.match(panel, /youtubeUploadAction\(connection\.status, current, focus\)/);
  assert.match(component, /YouTube: <strong[^>]*>\{lifecycleLabel\(upload\)\}/);
  assert.match(component, /scheduleLine\(current\.scheduled_for, zone, locale\)/);
  assert.match(component, /This project no longer exists\./);
});

test("an upload handed to the background keeps its row refreshing (bounded)", () => {
  const overview = (state: YouTubeUpload["current"]["state"] | null): QueueOverview => ({ run_started_at: null, youtube: { status: "connected", channel_title: "Lab" }, items: [{ job: rendered.job, project: project("A", { youtube: { current_render: { uploadable: false, code: null, message: null }, upload: state ? upload(state) : null } }) }] });
  assert.equal(uploadInFlight(overview("uploading")), true);
  assert.equal(uploadInFlight(overview("scheduled")), false);
  assert.equal(uploadInFlight(overview(null)), false);
  assert.equal(uploadInFlight(null), false);
  assert.match(component, /MAX_UPLOAD_FOLLOW_UPS/);
});

// ---------------------------------------------------------------------------
// Navigation
// ---------------------------------------------------------------------------

test("View Details opens the project's full page and the Studio queue links to /queue", () => {
  assert.match(component, /href=\{`\/projects\/\$\{job\.project_id\}`\}>View Details/);
  assert.match(component, /status\.state !== "unavailable"/);
  assert.match(route, /QueueOverviewPage/);
  assert.match(home, /<Link href="\/queue">[^]*Open Queue Overview/);
  assert.match(api, /"\/generation-jobs\/overview"/);
});

// ---------------------------------------------------------------------------
// Polling
// ---------------------------------------------------------------------------

test("the overview is re-read only when a job changes status or the job set changes", () => {
  const first = overviewRefreshNeeded(null, [job("A", "running", { progress: 0.1 }), job("B", "queued")]);
  assert.equal(first.refresh, false);
  const progress = overviewRefreshNeeded(first.signature, [job("A", "running", { progress: 0.6 }), job("B", "queued")]);
  assert.equal(progress.refresh, false);
  const finished = overviewRefreshNeeded(progress.signature, [job("A", "completed"), job("B", "running")]);
  assert.equal(finished.refresh, true);
  const added = overviewRefreshNeeded(finished.signature, [job("A", "completed"), job("B", "running"), job("C", "queued")]);
  assert.equal(added.refresh, true);
});

function fakeClock() {
  let now = 0;
  let nextId = 1;
  const pending = new Map<number, { at: number; callback: () => void }>();
  const timers: Timers = {
    setTimeout: (callback, ms) => { const id = nextId++; pending.set(id, { at: now + ms, callback }); return id; },
    clearTimeout: (handle) => { pending.delete(handle as number); },
  };
  const flush = () => new Promise<void>((resolve) => setImmediate(resolve));
  return {
    timers,
    now: () => now,
    pendingCount: () => pending.size,
    async advance(ms: number) {
      const target = now + ms;
      for (;;) {
        await flush();
        const due = [...pending.entries()].filter(([, item]) => item.at <= target).sort((a, b) => a[1].at - b[1].at)[0];
        if (!due) break;
        pending.delete(due[0]);
        now = due[1].at;
        due[1].callback();
      }
      now = target;
      await flush();
    },
  };
}

const visible: PageVisibility = { hidden: () => false, subscribe: () => () => {} };

test("queued → running → completed: one poller, and the finished row becomes previewable without a reload", async () => {
  const clock = fakeClock();
  const phases: GenerationJob[][] = [
    [job("A", "queued", { queue_position: 1 })],
    [job("A", "running", { progress: 0.4 })],
    [job("A", "running", { progress: 0.9 })],
    [job("A", "completed", { progress: 1 })],
  ];
  let tick = 0;
  let overviewLoads = 0;
  let shown: QueueOverview | null = null;
  let signature: string | null = null;
  const server = (): QueueOverview => ({
    run_started_at: "2026-10-01T09:00:00Z",
    youtube: { status: "connected", channel_title: "Lab" },
    items: [{ job: phases[Math.min(tick, phases.length - 1)][0], project: tick >= 3 ? project("A") : null }],
  });
  // The page's wiring: the same home-poll authority, overview refreshed on status changes only.
  const poller = createHomePoller<QueueOverview>({
    loadJobs: async () => phases[Math.min(tick++, phases.length - 1)],
    loadProjects: async () => { overviewLoads += 1; return server(); },
    onJobs: (jobs) => {
      const change = overviewRefreshNeeded(signature, jobs);
      signature = change.signature;
      if (change.refresh) void poller.refreshProjects();
    },
    onProjects: (overview) => { shown = overview; },
    timers: clock.timers,
    visibility: visible,
    now: clock.now,
    activeMs: 1000,
    idleMs: 60_000,
  });
  poller.start();
  await clock.advance(0);
  assert.equal(overviewLoads, 1);
  assert.equal(playableSource(shown!.items[0]), null);

  await clock.advance(1000); // queued -> running: status change, one overview read
  assert.equal(overviewLoads, 2);
  await clock.advance(1000); // progress only: no overview read
  assert.equal(overviewLoads, 2);
  await clock.advance(1000); // completed
  assert.equal(overviewLoads, 3);
  assert.equal(playableSource(shown!.items[0]), "/api/projects/A/final-video?revision=2");
  assert.equal(clock.pendingCount(), 1); // exactly one timer: no competing loop
  assert.equal(poller.mode, "idle");

  poller.stop();
  assert.equal(clock.pendingCount(), 0);
  await clock.advance(120_000);
  assert.equal(overviewLoads, 3);
});

test("the page owns one poller and stops it on unmount", () => {
  assert.equal(component.match(/createHomePoller/g)?.length, 2); // the import and the one instance
  assert.match(component, /instance\.stop\(\);/);
  assert.match(component, /return \(\) => window\.clearTimeout\(timer\)/);
  assert.doesNotMatch(component, /setInterval/);
});
