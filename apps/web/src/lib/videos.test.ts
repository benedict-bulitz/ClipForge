import assert from "node:assert/strict";
import test from "node:test";
import { existsSync, readFileSync } from "node:fs";
import {
  DEFAULT_FILTERS,
  analyticsValue,
  associationPairs,
  curveTicks,
  dateLine,
  detailedMetric,
  formatCount,
  formatPercent,
  formatViewDuration,
  libraryQuery,
  parseLibraryFilters,
  projectHref,
  projectLabel,
  retentionPolyline,
  sameFilters,
  sceneChange,
  stateTone,
  summaryLine,
  type LibrarySummary,
  type LibraryVideo,
  type RetentionPoint,
} from "./videos.ts";
import type { SceneRetention } from "./youtube.ts";

const read = (path: string) => readFileSync(new URL(path, import.meta.url), "utf8");
const library = read("../components/video-library.tsx");
const detail = read("../components/video-detail.tsx");
const api = read("./api.ts");
const home = read("../app/page.tsx");
const workspace = read("../components/project-workspace.tsx");
const settingsShell = read("../components/settings-shell.tsx");
const videosPage = read("../app/videos/page.tsx");
const videoPage = read("../app/videos/[id]/page.tsx");
const learningPage = read("../app/learning/page.tsx");
const connectionCard = read("../components/youtube-connection-card.tsx");

const video = (overrides: Partial<LibraryVideo> = {}): LibraryVideo => ({
  id: "u1", youtube_video_id: "vid00000001", title: "Why are airplane windows round?", prompt: "Why are airplane windows round?", topic: null,
  channel: { id: "UC1", title: "Knowledge Lab" }, state: "published", state_label: "Published", processing: false, stale: false, stale_reason: null,
  last_checked_at: "2026-09-10T12:00:00Z", scheduled_for: null, requested_publish_at: null, published_at: "2026-09-10T11:00:00Z",
  uploaded_at: "2026-09-09T10:00:00Z", sort_date: "2026-09-10T11:00:00Z", content_type: "SHORTS", duration_seconds: 10, format: "explanation", scene_count: 4,
  project: { id: "p1", available: true, title: "Why?", archived_at: null }, thumbnail_url: "/media/video-library/u1.webp",
  live_stats: { views: 900, likes: 40, comments: 3, checked_at: "2026-09-10T12:00:00Z", source: "youtube_data_api_videos_list" },
  analytics: { state: "available", fetched_at: "2026-09-10T12:00:00Z", views: 5400, engagedViews: 2100, averageViewDuration: 7.4, averageViewPercentage: 74, likes: 120, comments: 9 },
  watch_url: "https://www.youtube.com/watch?v=vid00000001", shorts_url: "https://www.youtube.com/shorts/vid00000001", studio_url: "https://studio.youtube.com/video/vid00000001/edit",
  ...overrides,
});

const scene = (overrides: Partial<SceneRetention> = {}): SceneRetention => ({
  index: 1, scene_id: "s1", story_role: "hook", start: 0, end: 3, duration: 3, status: "ok",
  retention_entering: 1, retention_leaving: 0.73, retention_delta: -0.27, average_retention: 0.85, ...overrides,
});

// ---------------------------------------------------------------------------
// Navigation and routes
// ---------------------------------------------------------------------------

test("Videos is a first-class destination reachable from every top-level screen", () => {
  assert.match(home, /href="\/videos"[^>]*>[\s\S]*?Videos/);
  assert.match(workspace, /href="\/videos" aria-label="Videos"/);
  assert.match(settingsShell, /href="\/videos"/);
  assert.match(library, /href="\/videos" aria-current="page"/);
  assert.match(videosPage, /<VideoLibrary initialFilters=\{parseLibraryFilters\(await searchParams\)\}/);
  assert.match(videoPage, /<VideoDetailPage key=\{id\} videoId=\{id\}/);
});

test("Learning History is not a second library: it redirects to archived Videos", () => {
  assert.match(learningPage, /redirect\("\/videos\?project=archived"\)/);
  assert.equal(existsSync(new URL("../components/learning-history.tsx", import.meta.url)), false);
  assert.doesNotMatch(api, /listLearningArchive|getLearningArchiveEntry/);
  assert.doesNotMatch(connectionCard, /href="\/learning"/);
  assert.match(connectionCard, /href="\/videos"/);
});

test("the index reads the library API only; YouTube is asked only on explicit refresh", () => {
  assert.match(api, /request<import\("\.\/videos"\)\.VideoLibraryPage>\(`\/videos\$\{query\}`/);
  assert.match(api, /"\/videos\/refresh-recent", \{ method: "POST" \}/);
  assert.doesNotMatch(library, /syncYouTubeUpload|refreshYouTubeAnalytics/);
  assert.match(library, /refreshRecentVideos\(\)/);
  assert.match(library, /SEARCH_DEBOUNCE_MS/);
  assert.match(library, /Load more/);
});

// ---------------------------------------------------------------------------
// Filters, query string, sorting options
// ---------------------------------------------------------------------------

test("filters come from the URL with safe fallbacks and produce minimal query strings", () => {
  assert.deepEqual(parseLibraryFilters({}), DEFAULT_FILTERS);
  assert.deepEqual(parseLibraryFilters({ project: "archived", status: "bogus", sort: "views", q: ["abc", "x"] }), { ...DEFAULT_FILTERS, project: "archived", sort: "views", q: "abc" });
  assert.equal(libraryQuery(DEFAULT_FILTERS), "");
  assert.equal(libraryQuery({ ...DEFAULT_FILTERS, status: "scheduled", q: "  volcano " }), "?status=scheduled&q=volcano");
  assert.equal(libraryQuery(DEFAULT_FILTERS, { limit: 24, offset: 48 }), "?limit=24&offset=48");
  assert.ok(sameFilters(DEFAULT_FILTERS, { ...DEFAULT_FILTERS, q: " " }));
  assert.ok(!sameFilters(DEFAULT_FILTERS, { ...DEFAULT_FILTERS, analytics: "processing" }));
  for (const option of ["Published", "Scheduled", "Private", "Deleted", "Project available", "Archived", "Available", "Processing", "Most views", "Avg view %", "Avg view duration", "Oldest"]) {
    assert.match(read("./videos.ts"), new RegExp(`"${option}"`));
  }
});

// ---------------------------------------------------------------------------
// Card data
// ---------------------------------------------------------------------------

test("missing metrics show a dash or Processing, never zero", () => {
  assert.equal(formatCount(null), "—");
  assert.equal(formatCount(0), "0");
  assert.equal(formatCount(12345), "12,345");
  assert.equal(formatPercent(undefined), "—");
  assert.equal(formatViewDuration(7.44), "7.4 s");
  assert.equal(formatViewDuration(75), "1:15");
  assert.equal(analyticsValue(null, "processing", formatPercent), "Processing");
  assert.equal(analyticsValue(null, "partial", formatPercent), "Processing");
  assert.equal(analyticsValue(null, "not_published", formatPercent), "—");
  assert.equal(analyticsValue(74, "available", formatPercent), "74.0%");
});

test("Open project exists only while the project exists", () => {
  assert.equal(projectHref(video()), "/projects/p1");
  assert.equal(projectLabel(video()), "Project available");
  const archived = video({ project: { id: "p1", available: false, title: "Why?", archived_at: "2026-09-12T00:00:00Z" } });
  assert.equal(projectHref(archived), null);
  assert.equal(projectLabel(archived), "Project deleted · Learning archive");
  assert.match(library, /\{project && <Link href=\{project\}/);
  assert.match(detail, /\{project && <Button asChild variant="outline" size="sm"><Link href=\{project\}>/);
  assert.match(detail, /Project deleted<\/span>[\s\S]*?Learning data retained/);
});

test("statuses and date lines", () => {
  assert.equal(stateTone("published"), "ok");
  assert.equal(stateTone("scheduled"), "info");
  assert.equal(stateTone("private"), "muted");
  assert.equal(stateTone("processing"), "warn");
  assert.equal(stateTone("deleted"), "error");
  assert.equal(stateTone("rejected"), "error");
  const format = (iso: string) => iso.slice(0, 10);
  assert.equal(dateLine(video(), format), "Published 2026-09-10");
  assert.equal(dateLine(video({ state: "scheduled", published_at: null, scheduled_for: "2030-01-01T18:00:00Z" }), format), "Scheduled for 2030-01-01");
  assert.equal(dateLine(video({ state: "private", published_at: null }), format), "Uploaded 2026-09-09");
  assert.equal(dateLine(video({ state: "deleted" }), format), "Published 2026-09-10");
});

test("the card identifies a video by its YouTube title, ID, channel, prompt and preview", () => {
  assert.match(library, /\{video\.title\}/);
  assert.match(library, /<span className="mono">\{video\.youtube_video_id\}<\/span>/);
  assert.match(library, /video\.channel\.title/);
  assert.match(library, /Prompt: \{video\.topic \?\? video\.prompt\}/);
  assert.match(library, /aria-label="No preview"/); // placeholder for archives without an image
  assert.match(library, /onError=\{\(\) => setFailed\(true\)\}/);
});

test("the summary is short and truthful", () => {
  const summary: LibrarySummary = {
    total: 50, published: 42, unlisted: 0, scheduled: 3, private: 5, processing: 0, deleted: 0, rejected: 0,
    analytics_available: 40, analytics_processing: 5, projects_available: 20, projects_archived: 30,
    median_average_view_percentage: 71.2, median_average_view_percentage_n: 40,
  };
  assert.deepEqual(summaryLine(summary), ["42 published", "3 scheduled", "5 private", "5 analytics processing"]);
  assert.deepEqual(summaryLine({ ...summary, private: 0, analytics_processing: 0 }), ["42 published", "3 scheduled"]);
  assert.match(library, /latest per video, n=\$\{summary\.median_average_view_percentage_n\}/);
});

// ---------------------------------------------------------------------------
// Detail
// ---------------------------------------------------------------------------

test("detail keeps live stats and detailed analytics apart", () => {
  assert.match(detail, /<Section title="Live stats" source="YouTube Data API">/);
  assert.match(detail, /<Section title="Detailed analytics" source="YouTube Analytics API">/);
  assert.match(detail, /current\.live_stats/);
  assert.match(detail, /DETAILED_METRICS\.map/);
  assert.equal(detailedMetric("averageViewPercentage", { value: 74, availability: "available", reason: null, source: "youtube_analytics_api" }, "available"), "74.0%");
  assert.equal(detailedMetric("averageViewDuration", { value: 7.4, availability: "available", reason: null, source: "youtube_analytics_api" }, "available"), "7.4 s");
  assert.equal(detailedMetric("shares", { value: null, availability: "no_data_yet", reason: null, source: "youtube_analytics_api" }, "available"), "Processing");
  assert.equal(detailedMetric("shares", { value: null, availability: "unavailable", reason: "x", source: "youtube_analytics_api" }, "available"), "—");
  assert.equal(detailedMetric("shares", undefined, "processing"), "Processing");
  assert.equal(detailedMetric("shares", undefined, "not_published"), "—");
});

test("detail has status, stale warning and the YouTube actions", () => {
  for (const text of ["Current YouTube state", "Scheduled time", "Published time", "Last remote status check", "Open on YouTube", "Open in YouTube Studio", "Refresh status", "Refresh analytics"]) {
    assert.match(detail, new RegExp(text));
  }
  assert.match(detail, /current\.stale &&/);
  assert.match(detail, /syncYouTubeUpload\(detail\.video\.id\)/);
  assert.match(detail, /refreshYouTubeAnalytics\(detail\.video\.id\)/);
  // no YouTube video deletion in this feature
  assert.doesNotMatch(detail, /deleteVideo|deleteYouTube|method: "DELETE"/);
  assert.doesNotMatch(api, /deleteYouTubeVideo|\/videos\/[^`"]*`, \{ method: "DELETE"/);
});

test("detail shows production and publishing context", () => {
  for (const text of ["Original prompt", "Format", "Scenes", "Verbal hook strategy", "Final verbal hook", "Visual hook intent", "On-screen hook", "Answer / reveal", "Final Critic issues", "Successful repairs", "Unresolved issues", "Schedule provenance", "Selected slot", "Timezone", "Smart Scheduler selected it"]) {
    assert.match(detail, new RegExp(text));
  }
  assert.match(detail, /Channel comparison/);
  assert.match(detail, /Sample size: n=\$\{performance\.baseline\.sample_size\}/);
  assert.match(detail, /Learning observations/);
  assert.match(detail, /Confidence: \{humanize\(item\.confidence\)\} · Sample size: n=\{item\.sample_size\}/);
  assert.match(detail, /do not show that a production choice caused/);
  assert.deepEqual(associationPairs({ hook_strategy: "curiosity_gap", scene_seconds: 3.4, overlay_present: true, missing: null }), [["Hook strategy", "Curiosity gap"], ["Scene seconds", "3.4"], ["Overlay present", "yes"]]);
});

// ---------------------------------------------------------------------------
// Retention
// ---------------------------------------------------------------------------

test("the retention curve uses exactly the stored points (no interpolation)", () => {
  const points: RetentionPoint[] = [0.01, 0.5, 1].map((ratio, index) => ({ elapsed_video_ratio: ratio, second: ratio * 10, audience_watch_ratio: [1, 0.7, 0.5][index], relative_retention_performance: null }));
  const line = retentionPolyline(points, 100, 50);
  assert.equal(line.points.split(" ").length, 3);
  assert.equal(line.points, "1.0,0.0 50.0,15.0 100.0,25.0");
  assert.equal(line.maxRatio, 1);
  const replay = retentionPolyline([{ elapsed_video_ratio: 0.01, second: 0.1, audience_watch_ratio: 1.25, relative_retention_performance: null }], 100, 50);
  assert.equal(replay.maxRatio, 1.25);
  assert.equal(replay.points, "1.0,0.0");
  assert.equal(retentionPolyline([], 100, 50).points, "");
  assert.match(detail, /<polyline points=\{points\}/);
  assert.doesNotMatch(detail, /<path|curveMonotone|catmull|bezier/i); // straight segments only
  assert.deepEqual(curveTicks(10).map((tick) => tick.label), ["0:00", "0:02", "0:05", "0:07", "0:10"]);
});

test("scene retention rows read like 'Scene 1 · Hook 0:00–0:03 100% → 73% Δ −27%'", () => {
  assert.deepEqual(sceneChange(scene()), { range: "100% → 73%", delta: "Δ −27%" });
  assert.deepEqual(sceneChange(scene({ retention_entering: 0.73, retention_leaving: 0.69, retention_delta: -0.04 })), { range: "73% → 69%", delta: "Δ −4%" });
  assert.deepEqual(sceneChange(scene({ retention_delta: null, retention_leaving: null })), { range: "100% → —", delta: "Δ —" });
  assert.match(detail, /\{sceneTitle\(scene\)\}/);
  assert.match(detail, /\{formatClock\(scene\.start\)\}–\{formatClock\(scene\.end\)\}/);
  assert.doesNotMatch(detail, /["'>]\s*bad\s*["'<]/i); // no scene is labelled "bad"
});
