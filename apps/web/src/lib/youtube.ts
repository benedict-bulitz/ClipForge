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
  current: CurrentStatus;
  next_status_check_in_seconds: number | null;
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

export type CurrentState = "uploading" | "upload_failed" | "deleted" | "rejected" | "processing_failed" | "published" | "unlisted" | "scheduled" | "publish_pending" | "private";

/** YouTube's reconciled current state (remote authority) plus the historical request. */
export type CurrentStatus = {
  state: CurrentState;
  label: string;
  processing: boolean;
  stale: boolean;
  stale_reason: "never_checked" | "refresh_failed" | "old" | null;
  last_checked_at: string | null;
  last_attempt_at: string | null;
  refresh_error: { code: string; message: string } | null;
  scheduled_for: string | null;
  published_at: string | null;
  published_time_source: string | null;
  first_observed_public_at: string | null;
  remote: { privacy_status: string | null; upload_status: string | null; processing_status: string | null; publish_at: string | null; published_at: string | null; rejection_reason: string | null; failure_reason: string | null };
  live_stats: { views: number | null; likes: number | null; comments: number | null; checked_at: string; source: string } | null;
  requested: { visibility: string; publish_at: string | null; local_time: string | null; timezone: string | null; history: Array<{ publish_at: string; local_time: string | null; timezone: string | null; replaced_at: string }> };
};

export type AnalyticsState = "not_published" | "processing" | "partial" | "available" | "failed" | "auth_error";

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
  analytics_state?: AnalyticsState;
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
    case "failed": return "The YouTube upload needs attention";
    case "deleted": return "This video no longer exists on YouTube";
    default: break;
  }
  switch (report.analytics_state) {
    case "not_published": return report.status === "scheduled" ? "Not published yet — detailed analytics start after YouTube publishes the video" : "Not published — detailed analytics start after publication";
    case "processing": return "Detailed analytics · Processing on YouTube";
    case "failed": return "Detailed analytics could not be loaded";
    case "auth_error": return "Reconnect YouTube to load detailed analytics";
    default: return report.status === "waiting_for_data" ? "Detailed analytics · Processing on YouTube" : "Performance";
  }
}

/** Non-promissory: YouTube's docs give no guaranteed delay, so none is claimed. */
export function analyticsReadiness(state: AnalyticsState | undefined): { title: string; body: string } | null {
  switch (state) {
    case "processing": return { title: "Processing on YouTube", body: "Retention and detailed watch metrics may take time to appear. Live views, likes and comments update sooner." };
    case "partial": return { title: "Partly available", body: "Some detailed metrics or the retention curve are still processing on YouTube." };
    case "failed": return { title: "Could not load", body: "YouTube Analytics did not answer. Try Refresh analytics later." };
    case "auth_error": return { title: "Sign-in needed", body: "Reconnect YouTube in Settings to load detailed analytics." };
    default: return null;
  }
}

export function relativeTime(iso: string, now: Date, locale: string): string {
  const seconds = Math.round((new Date(iso).getTime() - now.getTime()) / 1000);
  const format = new Intl.RelativeTimeFormat(locale, { numeric: "auto" });
  const abs = Math.abs(seconds);
  if (abs < 60) return format.format(seconds, "second");
  if (abs < 3600) return format.format(Math.round(seconds / 60), "minute");
  if (abs < 86400) return format.format(Math.round(seconds / 3600), "hour");
  return format.format(Math.round(seconds / 86400), "day");
}

/** "28.09.2026 · 20:30" in the given zone (24 h where the locale uses it). */
export function scheduleLine(iso: string, timezone: string, locale: string): string {
  const date = new Intl.DateTimeFormat(locale, { day: "2-digit", month: "2-digit", year: "numeric", timeZone: timezone }).format(new Date(iso));
  const time = new Intl.DateTimeFormat(locale, { hour: "2-digit", minute: "2-digit", timeZone: timezone, hourCycle: uses24HourClock(locale) ? "h23" : "h12" }).format(new Date(iso));
  return `${date} · ${time}`;
}

export function clockTime(iso: string, locale: string, timezone?: string): string {
  return new Intl.DateTimeFormat(locale, { hour: "2-digit", minute: "2-digit", timeZone: timezone, hourCycle: uses24HourClock(locale) ? "h23" : "h12" }).format(new Date(iso));
}

/** The dominant line: YouTube's current state, e.g. "Published · 17 minutes ago". */
export function currentHeadline(current: CurrentStatus, now: Date, locale: string): string {
  if ((current.state === "published" || current.state === "unlisted") && current.published_at) {
    return `${current.label} · ${relativeTime(current.published_at, now, locale)}`;
  }
  return current.label;
}

/** "Last confirmed 8 minutes ago · Could not refresh YouTube status" when stale. */
export function freshnessLine(current: CurrentStatus, now: Date, locale: string, timezone?: string): string | null {
  if (!current.last_checked_at) return current.stale_reason === "never_checked" ? "Not checked with YouTube yet" : null;
  const checked = `Last checked ${clockTime(current.last_checked_at, locale, timezone)}`;
  if (current.stale_reason === "refresh_failed") return `Last confirmed ${relativeTime(current.last_checked_at, now, locale)} · Could not refresh YouTube status`;
  if (current.stale_reason === "old") return `Last confirmed ${relativeTime(current.last_checked_at, now, locale)}`;
  return checked;
}

/**
 * Whether "Upload to YouTube" is offered for a project: the one rule for every
 * entry point into the publishing sheet (Results page, Queue Overview).  The
 * backend still re-checks everything when the sheet submits.
 */
export function youtubeUploadAction(connectionStatus: YouTubeConnection["status"], current: ProjectYouTube["current_render"], focus: YouTubeUpload | null): { canUpload: boolean; newRevision: boolean; blockedReason: string | null } {
  const connected = connectionStatus === "connected";
  const newRevision = !!focus?.youtube_video_id && current.uploadable && focus.render_revision !== current.render_revision;
  const canUpload = connected && current.uploadable && (!focus || !focus.youtube_video_id || newRevision);
  const blockedReason = !connected ? null : current.uploadable ? null : current.code === "already_uploaded" ? null : current.message;
  return { canUpload, newRevision, blockedReason };
}

export function lifecycleLabel(upload: YouTubeUpload): string {
  if (upload.lifecycle === "uploading") return upload.progress !== null ? `Uploading · ${Math.round(upload.progress * 100)}%` : "Uploading";
  return upload.current?.label ?? upload.lifecycle;
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
  /** "auto" = the Smart Slot Planner's slot; "manual" = the user's own time (never snapped back). */
  schedule_source?: "auto" | "manual" | null;
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
  /** Where the pre-selected settings came from: this channel's last successful upload, or saved Settings. */
  preset_source?: "last_upload" | "settings";
  /** Smart Slot Planner state for this channel (the next free slot, freshness, today's slots). */
  smart_schedule?: import("./youtube-schedule").SmartScheduleState;
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

export type Preflight = { issues: PreflightIssue[]; schedule: ScheduleResolution | null; schedule_conflict?: import("./youtube-schedule").ScheduleConflict | null; ready: boolean };

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

/** The browser/system IANA zone, e.g. "Europe/Berlin". */
export function detectTimeZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

/** A locale tag Intl accepts; browsers may report POSIX tags such as "en-US@posix". */
export function safeLocale(tag: string | null | undefined): string {
  if (!tag) return "en-US";
  try {
    return Intl.getCanonicalLocales(tag)[0] ?? "en-US";
  } catch {
    const base = tag.split(/[@.]/)[0].replace("_", "-");
    try {
      return Intl.getCanonicalLocales(base)[0] ?? "en-US";
    } catch {
      return "en-US";
    }
  }
}

export function browserLocale(): string {
  return safeLocale(typeof navigator !== "undefined" ? navigator.language : null);
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
  switch (upload.current.state) {
    case "published": return "Public";
    case "unlisted": return "Unlisted";
    case "scheduled": return "Scheduled";
    case "publish_pending": return "Private (scheduled time passed)";
    default: return "Private";
  }
}

export function videoStatusLabel(status: YouTubeUpload["video_status"]): string {
  return { uploading: "Uploading", uploaded: "Uploaded", processing: "Processing", ready: "Ready", failed: "Failed", deleted: "Deleted on YouTube" }[status];
}

export function thumbnailStatusLabel(thumbnail: YouTubeUpload["thumbnail"]): string {
  return { none: "–", pending: "Pending", applied: "Applied", failed: "Failed", not_requested: "YouTube's automatic frame" }[thumbnail.status];
}

/** Independent status rows from YouTube's current state; never one vague "Uploaded". */
export function statusRows(upload: YouTubeUpload, locale: string): Array<{ label: string; value: string; tone: "ok" | "warn" | "error" | "muted" }> {
  const current = upload.current;
  const zone = current.requested.timezone ?? "UTC";
  const schedule = current.state === "published" || current.state === "unlisted"
    ? current.published_at ? `Published ${scheduleLine(current.published_at, zone, locale)} · ${zone}` : "Published"
    : current.scheduled_for ? `${scheduleLine(current.scheduled_for, zone, locale)} · ${zone}` : "Not scheduled";
  const audienceValue = upload.audience.confirmed_by_youtube ?? upload.audience.made_for_kids;
  const video = current.state === "rejected" ? `Rejected${current.remote.rejection_reason ? ` (${current.remote.rejection_reason})` : ""}`
    : current.state === "processing_failed" ? `Processing failed${current.remote.failure_reason ? ` (${current.remote.failure_reason})` : ""}`
    : current.processing ? "Processing" : videoStatusLabel(upload.video_status);
  return [
    { label: "Video", value: video, tone: ["rejected", "processing_failed", "deleted"].includes(current.state) || upload.video_status === "failed" ? "error" : upload.video_status === "ready" ? "ok" : "muted" },
    { label: "Thumbnail", value: thumbnailStatusLabel(upload.thumbnail), tone: upload.thumbnail.status === "failed" ? "error" : upload.thumbnail.status === "applied" ? "ok" : "muted" },
    { label: "Audience", value: audienceLabel(audienceValue), tone: audienceValue === null ? "warn" : "ok" },
    { label: "Visibility", value: visibilityLabel(upload) + (upload.visibility_restricted ? " (kept private by YouTube)" : ""), tone: upload.visibility_restricted || current.state === "publish_pending" ? "warn" : current.state === "published" ? "ok" : "muted" },
    { label: "Schedule", value: upload.schedule.status === "schedule_failed" ? `Failed: ${upload.schedule.error ?? "unknown reason"}` : schedule, tone: upload.schedule.status === "schedule_failed" ? "error" : "muted" },
  ];
}

export function deleteDialogCopy(plan: DeletionPlan): { title: string; body: string; confirm: string } {
  switch (plan.mode) {
    case "archive":
      return {
        title: "Delete local project?",
        body: "This video is already linked to YouTube. ClipForge will delete the local video and media files but keep the compact performance/analytics record so future videos can learn from it. The video stays in Videos.",
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

// ---------------------------------------------------------------------------
// Publishing sheet progress (upload -> scheduling -> result)
// ---------------------------------------------------------------------------

export type PublishPhase = {
  phase: "uploading" | "scheduling" | "finishing" | "success" | "partial" | "error";
  /** Text of the primary button in this phase. */
  label: string;
  tone: "busy" | "success" | "warn" | "error";
  message: string | null;
  retry: "upload" | "schedule" | null;
  /** The sheet may close on its own (after SUCCESS_VISIBLE_MS). */
  close: boolean;
};

/** How long the green success state stays visible before the sheet closes. */
export const SUCCESS_VISIBLE_MS = 1400;
/** A partial success (e.g. thumbnail needs attention) stays longer so it can be read. */
export const PARTIAL_VISIBLE_MS = 3500;
/** How often the open sheet re-reads the upload while YouTube works. */
export const STATUS_POLL_MS = 1500;
/** After the video ID exists, wait this long for the thumbnail/status read-back. */
export const FINISH_GRACE_MS = 20_000;

/**
 * The sheet's state for one upload, derived from the backend's record only:
 * never "Scheduled ✓" unless YouTube confirmed the schedule.
 */
export function publishPhase(upload: YouTubeUpload, wantsSchedule: boolean, videoSeenAt: number | null, now: number): PublishPhase {
  const current = upload.current;
  if (upload.lifecycle === "uploading" || upload.state === "pending" || upload.state === "uploading") {
    const pct = upload.progress !== null ? ` ${Math.round(upload.progress * 100)}%` : "";
    return { phase: "uploading", label: `Uploading…${pct}`, tone: "busy", message: null, retry: null, close: false };
  }
  if (!upload.youtube_video_id) {
    return { phase: "error", label: "Retry upload", tone: "error", message: `Upload failed. ${upload.error?.message ?? "YouTube did not accept the upload."}`, retry: upload.error?.code === "session_expired_unknown_outcome" ? null : "upload", close: false };
  }
  if (["rejected", "processing_failed", "deleted"].includes(current.state)) {
    return { phase: "error", label: "Upload failed", tone: "error", message: `${current.label}. ${upload.error?.message ?? ""}`.trim(), retry: null, close: false };
  }
  const answered = !!(current.last_attempt_at || current.last_checked_at);
  const settled = upload.thumbnail.status !== "pending" && (answered || (videoSeenAt !== null && now - videoSeenAt > FINISH_GRACE_MS));
  if (!settled) {
    return wantsSchedule
      ? { phase: "scheduling", label: "Scheduling…", tone: "busy", message: null, retry: null, close: false }
      : { phase: "finishing", label: "Finishing…", tone: "busy", message: null, retry: null, close: false };
  }
  const thumbnailFailed = upload.thumbnail.status === "failed";
  if (wantsSchedule && upload.schedule_status === "schedule_failed") {
    return { phase: "partial", label: "Retry scheduling", tone: "warn", message: `Video uploaded, but scheduling failed. ${upload.schedule_error ?? ""}`.trim(), retry: "schedule", close: false };
  }
  if (wantsSchedule) {
    const confirmed = !!current.last_checked_at;
    const scheduled = ["scheduled", "publish_pending"].includes(current.state) && !!current.remote.publish_at;
    const live = current.state === "published" || current.state === "unlisted";
    if (confirmed && !scheduled && !live) {
      return { phase: "partial", label: "Retry scheduling", tone: "warn", message: "Video uploaded, but scheduling failed. YouTube did not keep the publication time.", retry: "schedule", close: false };
    }
    if (!confirmed) {
      return { phase: "partial", label: "Uploaded ✓", tone: "warn", message: "Video uploaded. YouTube has not confirmed the schedule yet — check the status after the sheet closes.", retry: null, close: true };
    }
    if (thumbnailFailed) return { phase: "partial", label: "Scheduled · thumbnail needs attention", tone: "warn", message: `Thumbnail could not be applied: ${upload.thumbnail.failure_reason ?? "unknown reason"}`, retry: null, close: true };
    return { phase: "success", label: "Scheduled ✓", tone: "success", message: "Scheduled on YouTube.", retry: null, close: true };
  }
  if (thumbnailFailed) return { phase: "partial", label: "Uploaded · thumbnail needs attention", tone: "warn", message: `Video uploaded successfully. Thumbnail could not be applied: ${upload.thumbnail.failure_reason ?? "unknown reason"}`, retry: null, close: true };
  return { phase: "success", label: "Uploaded ✓", tone: "success", message: "Uploaded to YouTube.", retry: null, close: true };
}
