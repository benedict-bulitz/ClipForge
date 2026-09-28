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
  video_status: "uploading" | "uploaded" | "processing" | "ready" | "failed" | "deleted";
  thumbnail: { status: "none" | "pending" | "applied" | "failed" | "not_requested"; source: string | null; asset: string | null; failure_reason: string | null; applied_at: string | null };
  audience: { made_for_kids: boolean | null; confirmed_by_youtube: boolean | null; answered: boolean };
  contains_synthetic_media: boolean | null;
  requested_visibility: string;
  visibility_restricted: boolean;
  schedule: { status: string; publish_at: string | null; local_time: string | null; timezone: string | null; error: string | null };
  source_kind: string | null;
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


// ---------------------------------------------------------------------------
// Publishing (V2)
// ---------------------------------------------------------------------------

export type Visibility = "private" | "schedule" | "unlisted" | "public";

export type ScheduleChoice = { date: string; time: string; timezone: string };

export type ThumbnailSelection = { source: "generated" | "custom" | "youtube_auto"; asset?: string | null };

export type PublishOptions = {
  title: string;
  description: string;
  tags: string[];
  thumbnail: ThumbnailSelection | null;
  made_for_kids: boolean | null;
  contains_synthetic_media: boolean | null;
  visibility: Visibility;
  schedule: ScheduleChoice | null;
  category_id: string | null;
  default_language: string | null;
  default_audio_language: string | null;
  license: "youtube" | "creativeCommon";
  embeddable: boolean;
  public_stats_viewable: boolean;
  notify_subscribers: boolean;
  recording_date: string | null;
  paid_product_placement: boolean;
};

export type UploadDefaults = {
  made_for_kids: boolean | null;
  contains_synthetic_media: boolean | null;
  category_id: string | null;
  default_language: string | null;
  license: "youtube" | "creativeCommon" | null;
  embeddable: boolean | null;
  public_stats_viewable: boolean | null;
  notify_subscribers: boolean | null;
  visibility: Visibility | null;
  timezone: string | null;
  api_project_audited: boolean;
};

export type ThumbnailOption = { source: "generated" | "custom"; asset: string; platform: string | null; label: string; url: string; width: number; height: number; valid: boolean; problem: string | null };

export type CatalogEntry = { key: string; label: string; api?: string; reason?: string };

export type PublishingDraft = {
  options: PublishOptions & { language?: string | null };
  defaults: UploadDefaults;
  thumbnails: ThumbnailOption[];
  categories: Array<{ id: string; title: string }>;
  category_error: { code: string; message: string } | null;
  suggested_category: { id: string; title: string; reason: string } | null;
  synthetic_suggestion: { value: boolean | null; why: string };
  allowed_visibilities: Visibility[];
  limits: { title: number; description_bytes: number; tags: number };
  catalog: { api_writable: CatalogEntry[]; studio_only: CatalogEntry[]; api_read_only: CatalogEntry[]; api_writable_not_exposed: CatalogEntry[] };
  render_status: ProjectYouTube["current_render"];
};

export type PreflightIssue = { field: string; message: string };

export type ScheduleResolution = {
  status: "ok" | "invalid" | "invalid_timezone" | "nonexistent" | "ambiguous" | "past" | "too_far";
  message: string | null;
  publish_at: string | null;
  local_time: string | null;
  timezone: string | null;
  abbreviation: string | null;
  utc_offset: string | null;
};

export type Preflight = { issues: PreflightIssue[]; schedule: ScheduleResolution | null; ready: boolean };

export type DeletionPlan = {
  project_id: string;
  title: string;
  mode: "full" | "archive" | "unverified" | "busy";
  reclaimable_bytes: number | null;
  retained_bytes: number;
  verification: "not_needed" | "live" | "offline";
  uploads: Array<YouTubeUpload & { classification: string }>;
  messages: string[];
};

export type ArchiveEntry = {
  upload_id: string;
  project_id: string;
  title: string;
  topic: string | null;
  archived_at: string | null;
  editable: false;
  upload: YouTubeUpload;
  summary: { views: number | null; engagedViews: number | null; averageViewPercentage: number | null; fetched_at: string | null };
};

/** The browser/system IANA zone, e.g. "Europe/Berlin". */
export function detectTimeZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

export function browserLocale(): string {
  return typeof navigator !== "undefined" && navigator.language ? navigator.language : "en-US";
}

/** "de-DE" -> "DE" (YouTube category region); falls back to US. */
export function regionFromLocale(locale: string): string {
  const region = locale.split("-").find((part, index) => index > 0 && /^[A-Za-z]{2}$/.test(part));
  return (region ?? "US").toUpperCase();
}

export function uses24HourClock(locale: string): boolean {
  const cycle = new Intl.DateTimeFormat(locale, { hour: "numeric" }).resolvedOptions().hourCycle;
  return cycle === "h23" || cycle === "h24";
}

/** "Europe/Berlin (CEST · UTC+02:00)" from the backend's tz-database resolution. */
export function zoneLabel(timezone: string, resolution?: Pick<ScheduleResolution, "abbreviation" | "utc_offset"> | null): string {
  if (resolution?.abbreviation && resolution.utc_offset) return `${timezone} (${resolution.abbreviation} · ${resolution.utc_offset})`;
  try {
    const part = new Intl.DateTimeFormat("en-US", { timeZone: timezone, timeZoneName: "longOffset" })
      .formatToParts(new Date())
      .find((item) => item.type === "timeZoneName")?.value;
    const offset = part === "GMT" ? "UTC+00:00" : part?.replace("GMT", "UTC");
    return offset ? `${timezone} (${offset})` : timezone;
  } catch {
    return timezone;
  }
}

/** "28.09.2026" / "09/28/2026" - the date as the user's locale writes it. */
export function formatLocalDate(date: string, locale: string): string {
  const [year, month, day] = date.split("-").map(Number);
  if (!year || !month || !day) return date;
  return new Intl.DateTimeFormat(locale, { day: "2-digit", month: "2-digit", year: "numeric", timeZone: "UTC" }).format(new Date(Date.UTC(year, month - 1, day)));
}

/** "Monday, 28 September 2026 at 20:30" in the chosen zone (24 h where the locale uses it). */
export function formatScheduleConfirmation(publishAtIso: string, timezone: string, locale: string): string {
  return new Intl.DateTimeFormat(locale, {
    weekday: "long", day: "numeric", month: "long", year: "numeric",
    hour: "2-digit", minute: "2-digit", timeZone: timezone, hourCycle: uses24HourClock(locale) ? "h23" : "h12",
  }).format(new Date(publishAtIso));
}

export function primaryActionLabel(visibility: Visibility): string {
  return { private: "Upload privately", schedule: "Upload and schedule", unlisted: "Upload as unlisted", public: "Upload and publish" }[visibility];
}

export function attentionSummary(issues: PreflightIssue[]): string | null {
  if (!issues.length) return null;
  return `${issues.length} item${issues.length === 1 ? " needs" : "s need"} attention:`;
}

export function textLength(value: string): number {
  return Array.from(value).length;
}

export function utf8Bytes(value: string): number {
  return new TextEncoder().encode(value).length;
}

export function tagsLength(tags: string[]): number {
  return tags.reduce((sum, tag) => sum + tag.length + (tag.includes(" ") ? 2 : 0), 0) + Math.max(0, tags.length - 1);
}

export function parseTags(value: string): string[] {
  return value.split(",").map((tag) => tag.trim().replace(/^#/, "")).filter(Boolean);
}

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return "unknown";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) { value /= 1024; index += 1; }
  return `${index === 0 ? value : value.toFixed(1)} ${units[index]}`;
}

export function audienceLabel(value: boolean | null | undefined): string {
  return value === true ? "Made for kids" : value === false ? "Not made for kids" : "Not answered";
}

export function visibilityLabel(upload: YouTubeUpload): string {
  if (upload.lifecycle === "scheduled") return "Scheduled";
  return ({ private: "Private", public: "Public", unlisted: "Unlisted" } as Record<string, string>)[upload.privacy_status] ?? upload.privacy_status;
}

export function videoStatusLabel(status: YouTubeUpload["video_status"]): string {
  return { uploading: "Uploading", uploaded: "Uploaded", processing: "Processing", ready: "Ready", failed: "Failed", deleted: "Deleted on YouTube" }[status];
}

export function thumbnailStatusLabel(thumbnail: YouTubeUpload["thumbnail"]): string {
  return { none: "–", pending: "Pending", applied: "Applied", failed: "Failed", not_requested: "YouTube's automatic frame" }[thumbnail.status];
}

/** Independent status rows; never one vague "Uploaded". */
export function statusRows(upload: YouTubeUpload, locale: string): Array<{ label: string; value: string; tone: "ok" | "warn" | "error" | "muted" }> {
  const schedule = upload.schedule.publish_at && upload.schedule.timezone
    ? `${formatScheduleConfirmation(upload.schedule.publish_at, upload.schedule.timezone, locale)} · ${upload.schedule.timezone}`
    : upload.schedule.publish_at ? formatScheduleConfirmation(upload.schedule.publish_at, "UTC", locale) + " · UTC" : "Not scheduled";
  const audienceValue = upload.audience.confirmed_by_youtube ?? upload.audience.made_for_kids;
  return [
    { label: "Video", value: videoStatusLabel(upload.video_status), tone: upload.video_status === "failed" || upload.video_status === "deleted" ? "error" : upload.video_status === "ready" ? "ok" : "muted" },
    { label: "Thumbnail", value: thumbnailStatusLabel(upload.thumbnail), tone: upload.thumbnail.status === "failed" ? "error" : upload.thumbnail.status === "applied" ? "ok" : "muted" },
    { label: "Audience", value: audienceLabel(audienceValue), tone: audienceValue === null ? "warn" : "ok" },
    { label: "Visibility", value: visibilityLabel(upload) + (upload.visibility_restricted ? " (kept private by YouTube)" : ""), tone: upload.visibility_restricted ? "warn" : "muted" },
    { label: "Schedule", value: upload.schedule.status === "schedule_failed" ? `Failed: ${upload.schedule.error ?? "unknown reason"}` : schedule, tone: upload.schedule.status === "schedule_failed" ? "error" : "muted" },
  ];
}

export function deleteDialogCopy(plan: DeletionPlan): { title: string; body: string; confirm: string } {
  switch (plan.mode) {
    case "archive":
      return {
        title: "Delete local project?",
        body: "This video is already linked to YouTube. ClipForge will delete the local video and media files but keep the compact performance/analytics record so future videos can learn from it.",
        confirm: "Delete local project",
      };
    case "unverified":
      return {
        title: "Upload status could not be verified.",
        body: "ClipForge cannot confirm whether the last upload reached YouTube. Retry the check, or delete everything permanently if you are sure the video is not on YouTube.",
        confirm: "Delete permanently anyway",
      };
    case "busy":
      return { title: "Project is busy", body: plan.messages[0] ?? "Try again when the current work finishes.", confirm: "Delete permanently" };
    default:
      return {
        title: "Delete project permanently?",
        body: "This video was never uploaded to YouTube. All project data and local media will be deleted.",
        confirm: "Delete permanently",
      };
  }
}
