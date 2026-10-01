/**
 * Video Library: shapes of /api/videos and pure presentation helpers.
 *
 * Videos are the uploaded output + its performance history; they are
 * independent of editable projects (a deleted project keeps its video here).
 */
import type { AnalyticsState, CurrentStatus, MetricValue, PerformanceReport, SceneRetention, YouTubeUpload } from "./youtube";

export type LibraryState = "published" | "unlisted" | "scheduled" | "private" | "processing" | "deleted" | "rejected" | "processing_failed";

export type LibraryVideo = {
  id: string;
  youtube_video_id: string;
  title: string;
  prompt: string | null;
  topic: string | null;
  channel: { id: string; title: string | null };
  state: LibraryState;
  state_label: string;
  processing: boolean;
  stale: boolean;
  stale_reason: CurrentStatus["stale_reason"];
  last_checked_at: string | null;
  scheduled_for: string | null;
  requested_publish_at: string | null;
  /** The zone the schedule was chosen in; null for times set outside ClipForge. */
  schedule_timezone: string | null;
  published_at: string | null;
  uploaded_at: string | null;
  sort_date: string;
  content_type: string | null;
  duration_seconds: number | null;
  format: string | null;
  scene_count: number | null;
  project: { id: string; available: boolean; title: string | null; archived_at: string | null };
  thumbnail_url: string | null;
  /** YouTube Data API (videos.list statistics); null until the video has been public. */
  live_stats: CurrentStatus["live_stats"];
  live_stats_state: LiveStatsState;
  /** YouTube Analytics API, latest snapshot with data. */
  analytics: {
    state: AnalyticsState;
    fetched_at: string | null;
    views: number | null;
    engagedViews: number | null;
    averageViewDuration: number | null;
    averageViewPercentage: number | null;
    likes: number | null;
    comments: number | null;
  };
  /** Whether the remote video exists; independent of the ClipForge project. */
  youtube_actions: { available: boolean; reason: string | null };
  watch_url: string | null;
  shorts_url: string | null;
  studio_url: string | null;
};

/**
 * not_published: never public - YouTube's counters are placeholders, not audience numbers;
 * not_reported: public, but no statistics stored yet; available: YouTube's values (0 is a real 0).
 */
export type LiveStatsState = "not_published" | "not_reported" | "available";

export type StatusFilter = "all" | "published" | "unlisted" | "scheduled" | "private" | "processing" | "deleted" | "rejected";
export type ProjectFilter = "all" | "available" | "archived";
export type AnalyticsFilter = "all" | "available" | "processing";
export type LibrarySort = "newest" | "oldest" | "views" | "average_view_percentage" | "average_view_duration";

export type LibraryFilters = { status: StatusFilter; project: ProjectFilter; analytics: AnalyticsFilter; q: string; sort: LibrarySort };

export type LibrarySummary = {
  total: number;
  published: number;
  unlisted: number;
  scheduled: number;
  private: number;
  processing: number;
  deleted: number;
  rejected: number;
  analytics_available: number;
  analytics_processing: number;
  projects_available: number;
  projects_archived: number;
  median_average_view_percentage: number | null;
  median_average_view_percentage_n: number;
};

export type VideoLibraryPage = {
  items: LibraryVideo[];
  total: number;
  limit: number;
  offset: number;
  next_offset: number | null;
  filters: LibraryFilters & { query: string };
  summary: LibrarySummary;
  connection: { channel_id: string | null; channel_title: string | null; status: string };
};

export type RetentionPoint = { elapsed_video_ratio: number; second: number | null; audience_watch_ratio: number; relative_retention_performance: number | null };

export type ProductionContext = {
  available: boolean;
  prompt: string | null;
  topic: string | null;
  format: string | null;
  duration_seconds: number | null;
  scene_count: number | null;
  mean_scene_seconds: number | null;
  hook_strategy: string | null;
  verbal_hook: string | null;
  visual_hook: { subject: string | null; visual_strategy: string | null; visual_goal: string | null } | null;
  on_screen_hook: string | null;
  answer_reveal_seconds: number | null;
  payoff_seconds: number | null;
  critic: { status: string | null; issue_count: number | null; repairs_attempted: number | null; repairs_successful: number | null; unresolved_issue_count: number | null };
  media_origin_counts: Record<string, number> | null;
};

export type PublishingContext = {
  requested_visibility: string;
  requested_publish_at: string | null;
  schedule_source: "auto" | "manual" | null;
  provenance: string;
  smart_scheduler_selected: boolean;
  slot_time: string | null;
  local_time: string | null;
  timezone: string | null;
  schedule_status: string;
  uploaded_at: string | null;
};

export type VideoDetail = {
  video: LibraryVideo;
  upload: YouTubeUpload;
  performance: PerformanceReport & {
    baseline?: { sample_size: number; min_sample: number; sufficient: boolean };
    retention?: { snapshot_id: string | null; fetched_at: string | null; point_count: number };
  };
  retention_curve: RetentionPoint[];
  major_drops: SceneRetention[];
  production: ProductionContext;
  publishing: PublishingContext;
};

export const DEFAULT_FILTERS: LibraryFilters = { status: "all", project: "all", analytics: "all", q: "", sort: "newest" };
export const PAGE_SIZE = 24;

export const STATUS_OPTIONS: Array<[StatusFilter, string]> = [
  ["all", "All"],
  ["published", "Published"],
  ["scheduled", "Scheduled"],
  ["private", "Private"],
  ["deleted", "Deleted"],
  ["unlisted", "Unlisted"],
  ["processing", "Processing"],
  ["rejected", "Rejected"],
];
export const PROJECT_OPTIONS: Array<[ProjectFilter, string]> = [["all", "All projects"], ["available", "Project available"], ["archived", "Archived"]];
export const ANALYTICS_OPTIONS: Array<[AnalyticsFilter, string]> = [["all", "All analytics"], ["available", "Available"], ["processing", "Processing"]];
export const SORT_OPTIONS: Array<[LibrarySort, string]> = [
  ["newest", "Newest"],
  ["oldest", "Oldest"],
  ["views", "Most views"],
  ["average_view_percentage", "Avg view %"],
  ["average_view_duration", "Avg view duration"],
];

function pick<T extends string>(value: unknown, options: Array<[T, string]>, fallback: T): T {
  const text = Array.isArray(value) ? value[0] : value;
  return options.some(([key]) => key === text) ? (text as T) : fallback;
}

/** Filters from the URL (?status=…&project=archived…); unknown values fall back to defaults. */
export function parseLibraryFilters(params: Record<string, string | string[] | undefined>): LibraryFilters {
  const q = params.q;
  return {
    status: pick(params.status, STATUS_OPTIONS, "all"),
    project: pick(params.project, PROJECT_OPTIONS, "all"),
    analytics: pick(params.analytics, ANALYTICS_OPTIONS, "all"),
    q: (Array.isArray(q) ? q[0] : q ?? "").slice(0, 200),
    sort: pick(params.sort, SORT_OPTIONS, "newest"),
  };
}

/** Only non-default values, so the plain /videos URL stays plain. */
export function libraryQuery(filters: LibraryFilters, extra: Record<string, number> = {}): string {
  const params = new URLSearchParams();
  (Object.keys(DEFAULT_FILTERS) as Array<keyof LibraryFilters>).forEach((key) => {
    const value = key === "q" ? filters.q.trim() : filters[key];
    if (value && value !== DEFAULT_FILTERS[key]) params.set(key, value);
  });
  Object.entries(extra).forEach(([key, value]) => params.set(key, String(value)));
  const text = params.toString();
  return text ? `?${text}` : "";
}

export function sameFilters(a: LibraryFilters, b: LibraryFilters): boolean {
  return a.status === b.status && a.project === b.project && a.analytics === b.analytics && a.q.trim() === b.q.trim() && a.sort === b.sort;
}

/** "—" for a value YouTube has not reported; never a fabricated zero. */
export function formatCount(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : Math.round(value).toLocaleString("en-US");
}

export function formatPercent(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : `${value.toFixed(1)}%`;
}

export function formatViewDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "—";
  return seconds < 60 ? `${seconds.toFixed(1)} s` : `${Math.floor(seconds / 60)}:${String(Math.round(seconds % 60)).padStart(2, "0")}`;
}

export function formatClock(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "—";
  const safe = Math.max(0, seconds);
  return `${Math.floor(safe / 60)}:${String(Math.floor(safe % 60)).padStart(2, "0")}`;
}

/** A detailed-analytics value: the number, "Processing" while YouTube prepares it, else "—". */
export function analyticsValue(value: number | null | undefined, state: AnalyticsState, format: (value: number) => string): string {
  if (value !== null && value !== undefined) return format(value);
  return state === "processing" || state === "partial" ? "Processing" : "—";
}

export function analyticsStateLabel(state: AnalyticsState): string {
  return {
    not_published: "Starts after publication",
    processing: "Processing",
    partial: "Partial",
    available: "Available",
    failed: "Unavailable",
    auth_error: "Auth error",
  }[state];
}

export function stateTone(state: LibraryState): "ok" | "info" | "muted" | "warn" | "error" {
  switch (state) {
    case "published": return "ok";
    case "scheduled": return "info";
    case "unlisted": case "private": return "muted";
    case "processing": return "warn";
    default: return "error";
  }
}

/**
 * "Published 12 Sep 2026" / "Scheduled for 01.01.2030 · 19:00 · Europe/Berlin" /
 * "Uploaded …" - the card's date line; the exact scheduled time uses ``scheduled``.
 */
export function dateLine(video: LibraryVideo, format: (iso: string) => string, scheduled: (iso: string) => string = format): string {
  if (video.published_at && (video.state === "published" || video.state === "unlisted" || video.state === "deleted")) return `Published ${format(video.published_at)}`;
  if (video.scheduled_for) return `Scheduled for ${scheduled(video.scheduled_for)}`;
  return video.uploaded_at ? `Uploaded ${format(video.uploaded_at)}` : "—";
}

/** A live statistic, or "—" when YouTube has no audience number for it yet (never a placeholder 0). */
export function liveStatValue(video: Pick<LibraryVideo, "live_stats" | "live_stats_state">, key: "views" | "likes" | "comments"): string {
  return video.live_stats_state === "available" && video.live_stats ? formatCount(video.live_stats[key]) : "—";
}

export function liveStatsNote(state: LiveStatsState): string | null {
  if (state === "not_published") return "Starts after publication";
  if (state === "not_reported") return "Not reported by YouTube yet";
  return null;
}

/** The Open project link only exists while the project does (never a 404 link). */
export function projectHref(video: Pick<LibraryVideo, "project">): string | null {
  return video.project.available ? `/projects/${video.project.id}` : null;
}

export function projectLabel(video: Pick<LibraryVideo, "project">): string {
  return video.project.available ? "Project available" : "Project deleted · Learning archive";
}

export function summaryLine(summary: LibrarySummary): string[] {
  const parts = [`${summary.published} published`, `${summary.scheduled} scheduled`];
  if (summary.private) parts.push(`${summary.private} private`);
  if (summary.analytics_processing) parts.push(`${summary.analytics_processing} analytics processing`);
  return parts;
}

/** Scene row text: "100% → 73%" and "Δ −27%" (from the existing scene mapping). */
export function sceneChange(scene: SceneRetention): { range: string; delta: string } {
  const pct = (value: number | null) => (value === null ? "—" : `${Math.round(value * 100)}%`);
  const points = scene.retention_delta === null ? null : Math.round(scene.retention_delta * 100);
  const delta = points === null ? "Δ —" : `Δ ${points > 0 ? "+" : points < 0 ? "−" : "±"}${Math.abs(points)}%`;
  return { range: `${pct(scene.retention_entering)} → ${pct(scene.retention_leaving)}`, delta };
}

/**
 * SVG polyline points for the stored raw retention buckets: one vertex per
 * point, straight segments only (nothing is smoothed or interpolated).
 * The y scale tops out at the larger of 100% and the curve's maximum,
 * because Shorts replays can push audienceWatchRatio above 1.
 */
export function retentionPolyline(points: RetentionPoint[], width: number, height: number): { points: string; maxRatio: number } {
  const usable = points.filter((point) => Number.isFinite(point.audience_watch_ratio) && Number.isFinite(point.elapsed_video_ratio));
  const maxRatio = Math.max(1, ...usable.map((point) => point.audience_watch_ratio));
  const coords = usable.map((point) => {
    const x = Math.min(1, Math.max(0, point.elapsed_video_ratio)) * width;
    const y = height - (Math.max(0, point.audience_watch_ratio) / maxRatio) * height;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  return { points: coords.join(" "), maxRatio };
}

export function humanize(value: string | null | undefined): string {
  if (!value) return "—";
  const text = value.replaceAll("_", " ").trim();
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/** Detailed analytics (YouTube Analytics API) in the detail view; never a fabricated zero. */
export const DETAILED_METRICS: Array<[string, string]> = [
  ["engagedViews", "Engaged views"],
  ["estimatedMinutesWatched", "Minutes watched"],
  ["averageViewDuration", "Avg view duration"],
  ["averageViewPercentage", "Avg view %"],
  ["shares", "Shares"],
  ["subscribersGained", "Subscribers gained"],
  ["subscribersLost", "Subscribers lost"],
];

export function detailedMetric(name: string, metric: MetricValue | undefined, state: AnalyticsState | undefined): string {
  if (metric && metric.availability === "available" && metric.value !== null) {
    if (name === "averageViewDuration") return formatViewDuration(metric.value);
    if (name === "averageViewPercentage") return formatPercent(metric.value);
    return formatCount(metric.value);
  }
  if (metric?.availability === "no_data_yet" || state === "processing" || state === "partial") return "Processing";
  return "—";
}

/** "~3 s" style labels for the curve's x axis from the video duration. */
export function curveTicks(duration: number | null | undefined): Array<{ ratio: number; label: string }> {
  if (!duration || duration <= 0) return [0, 0.5, 1].map((ratio) => ({ ratio, label: `${Math.round(ratio * 100)}%` }));
  return [0, 0.25, 0.5, 0.75, 1].map((ratio) => ({ ratio, label: formatClock(ratio * duration) }));
}

/** Associated production data as readable "key: value" pairs (no causal wording). */
export function associationPairs(data: Record<string, unknown>): Array<[string, string]> {
  return Object.entries(data)
    .filter(([, value]) => value !== null && value !== undefined && value !== "")
    .map(([key, value]) => [humanize(key), typeof value === "number" ? String(Math.round(value * 100) / 100) : typeof value === "boolean" ? (value ? "yes" : "no") : humanize(String(value))]);
}

/** "Checked 10 recent videos with YouTube · Analytics for 1 older video." */
export function refreshNotice(result: { checked: number; analytics_due?: number; error: { message: string } | null }): string {
  const due = result.analytics_due ?? 0;
  const recent = result.checked - due;
  const parts = [`Checked ${recent} recent video${recent === 1 ? "" : "s"}${result.error ? "" : " with YouTube"}`];
  if (due > 0) parts.push(`Analytics for ${due} older video${due === 1 ? "" : "s"}`);
  if (result.error) parts.push(result.error.message);
  return `${parts.join(" · ")}${result.error ? "" : "."}`;
}
