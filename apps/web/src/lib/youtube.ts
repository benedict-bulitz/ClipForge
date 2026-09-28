/** YouTube Learning Loop: API shapes and pure presentation helpers. */

export type YouTubeCapabilities = Partial<Record<"upload" | "read" | "schedule" | "analytics", boolean>>;

export type YouTubeConnection = {
  status: "not_connected" | "connected" | "auth_expired";
  channel_id: string | null;
  channel_title: string | null;
  channel_url?: string;
  connected_at?: string;
  previous_channel_id?: string | null;
  capabilities: YouTubeCapabilities;
  client: { configured: boolean; client_id_source: "keyring" | "environment" | null; client_secret_configured: boolean; redirect_uri: string };
  error: { code: string; message: string } | null;
};

export type YouTubeLifecycle = "uploading" | "private" | "scheduled" | "published" | "failed" | "deleted";

export type YouTubeUpload = {
  id: string;
  project_id: string;
  project_revision: number;
  render_revision: number;
  render_sha256: string;
  channel_id: string;
  youtube_video_id: string | null;
  state: "pending" | "uploading" | "uploaded" | "processing" | "ready" | "failed";
  lifecycle: YouTubeLifecycle;
  privacy_status: string;
  publish_at: string | null;
  schedule_status: "none" | "scheduled" | "schedule_failed" | "published";
  schedule_error: string | null;
  published_at: string | null;
  upload_status: string | null;
  processing_status: string | null;
  failure_reason: string | null;
  rejection_reason: string | null;
  content_type: string | null;
  deleted_on_youtube: boolean;
  title: string;
  progress: number | null;
  error: { code: string; message: string } | null;
  is_active_mapping: boolean;
  can_reupload: boolean;
  uploaded_at: string | null;
  last_analytics_sync_at: string | null;
  watch_url: string | null;
  shorts_url: string | null;
  studio_url: string | null;
};

export type MetricValue = { value: number | null; availability: "available" | "unavailable" | "no_data_yet"; reason: string | null; source: string; note?: string | null };

export type SceneRetention = {
  index: number;
  scene_id: string;
  story_role: string | null;
  start: number;
  end: number;
  duration: number;
  status: "ok" | "below_bucket_resolution";
  retention_entering: number | null;
  retention_leaving: number | null;
  retention_delta: number | null;
  notable_drop?: boolean;
  average_retention: number | null;
  stopped_watching_index?: number | null;
};

export type OpeningRetention = {
  status: "ok" | "no_data";
  label: string;
  bucket_seconds: number | null;
  points: Array<{ target_second: number; status?: string; bucket_second?: number; audience_watch_ratio?: number | null }>;
  hook: { strategy?: string | null; verbal_hook?: string | null; on_screen_hook?: string | null };
};

export type EvidenceRecord = {
  kind: string;
  observation: string;
  associated_production_data: Record<string, unknown>;
  confidence: "low" | "medium" | "high";
  sample_size: number;
};

export type PerformanceReport = {
  status: "not_uploaded" | "uploading" | "private" | "scheduled" | "waiting_for_data" | "ready" | "failed" | "deleted";
  upload?: YouTubeUpload;
  last_analytics_sync_at?: string | null;
  analytics_error?: { code: string; message: string } | null;
  latest_snapshot?: { fetched_at: string; published_age_hours: number | null; content_type: string | null; metrics: Record<string, MetricValue> } | null;
  opening_retention?: OpeningRetention;
  scene_retention?: SceneRetention[];
  classification?: { label: string; reason?: string };
  baseline?: { sample_size: number; min_sample: number; sufficient: boolean };
  evidence?: EvidenceRecord[];
};

export type ProjectYouTube = {
  connection: YouTubeConnection;
  current_render: { uploadable: boolean; code: string | null; message: string | null; render_revision?: number; existing_upload_id?: string | null };
  uploads: YouTubeUpload[];
  focus_upload_id: string | null;
  performance: PerformanceReport;
};

export const PERFORMANCE_METRICS: Array<[string, string]> = [
  ["views", "Views"],
  ["engagedViews", "Engaged views"],
  ["averageViewDuration", "Avg view duration"],
  ["averageViewPercentage", "Avg view %"],
  ["likes", "Likes"],
  ["comments", "Comments"],
  ["shares", "Shares"],
];

export function formatSeconds(seconds: number): string {
  const safe = Math.max(0, seconds);
  const minutes = Math.floor(safe / 60);
  const rest = Math.floor(safe % 60);
  return `${minutes}:${String(rest).padStart(2, "0")}`;
}

export function formatRatio(value: number | null | undefined): string {
  return value === null || value === undefined ? "–" : `${Math.round(value * 100)}%`;
}

export function formatDelta(value: number | null | undefined): string {
  if (value === null || value === undefined) return "–";
  const points = Math.round(value * 100);
  return `Δ ${points > 0 ? "+" : points < 0 ? "−" : "±"}${Math.abs(points)}%`;
}

/** A metric value as the API gave it; unavailable metrics are never shown as zero. */
export function formatMetric(name: string, metric: MetricValue | undefined): string {
  if (!metric || metric.availability !== "available" || metric.value === null) return metric?.availability === "no_data_yet" ? "not yet" : "unavailable";
  if (name === "averageViewDuration") return `${metric.value.toFixed(1)} s`;
  if (name === "averageViewPercentage") return `${metric.value.toFixed(1)}%`;
  return Math.round(metric.value).toLocaleString("en-US");
}

export function performanceHeadline(report: PerformanceReport): string {
  switch (report.status) {
    case "not_uploaded": return "Not uploaded to YouTube";
    case "uploading": return "Uploading to YouTube…";
    case "private": return "Uploaded privately — analytics will become useful after publication";
    case "scheduled": return "Scheduled — analytics will become useful after publication";
    case "waiting_for_data": return "Waiting for YouTube analytics";
    case "failed": return "The YouTube upload needs attention";
    case "deleted": return "This video no longer exists on YouTube";
    case "ready": return "Performance";
  }
}

export function lifecycleLabel(upload: YouTubeUpload): string {
  switch (upload.lifecycle) {
    case "uploading": return upload.progress !== null ? `Uploading · ${Math.round(upload.progress * 100)}%` : "Uploading";
    case "private": return upload.state === "processing" ? "Private on YouTube · processing" : "Private on YouTube";
    case "scheduled": return "Scheduled";
    case "published": return "Published";
    case "failed": return "Upload failed";
    case "deleted": return "Deleted on YouTube";
  }
}

export function classificationLabel(label: string | undefined): string {
  return ({
    insufficient_data: "Not enough comparable videos yet",
    below_channel_baseline: "Below your channel baseline",
    near_channel_baseline: "Near your channel baseline",
    above_channel_baseline: "Above your channel baseline",
    outlier_positive: "Positive outlier for your channel",
  } as Record<string, string>)[label ?? ""] ?? "–";
}

/** "Scene 1 · Hook" */
export function sceneTitle(scene: SceneRetention): string {
  const role = (scene.story_role ?? "").replaceAll("_", " ").trim();
  return role ? `Scene ${scene.index} · ${role.charAt(0).toUpperCase()}${role.slice(1)}` : `Scene ${scene.index}`;
}

/** Only scenes with real bucket data are shown. */
export function visibleSceneRows(scenes: SceneRetention[] | undefined): SceneRetention[] {
  return (scenes ?? []).filter((scene) => scene.status === "ok" && scene.retention_entering !== null && scene.retention_leaving !== null);
}

/** A local <input type="date"> + <input type="time"> choice as an absolute ISO instant. */
export function localDateTimeToIso(date: string, time: string): string | null {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date) || !/^\d{2}:\d{2}$/.test(time)) return null;
  const value = new Date(`${date}T${time}:00`);
  return Number.isNaN(value.getTime()) ? null : value.toISOString();
}

export function formatDateTime(value: string | null | undefined): string {
  if (!value) return "–";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "–" : date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}
