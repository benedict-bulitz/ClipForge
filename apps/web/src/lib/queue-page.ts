/**
 * Queue Overview (/queue): shapes of GET /api/generation-jobs/overview and the
 * pure presentation rules of its rows.
 *
 * The backend decides which jobs form the current queue (the latest queue run,
 * see apps/api/clipforge/queue_overview.py) and every project fact shown here;
 * this module only maps those facts to labels.  Live job progress comes from the
 * page's one poller (home-poll.ts), which re-reads the overview only when a job
 * changes status or the job set changes.
 */
import type { FinalQualityReview, GenerationJob, ProjectState } from "./types";
import type { ProjectPublication } from "./publishing";
import type { ProjectYouTube, YouTubeConnection, YouTubeUpload } from "./youtube";
import { qualityReviewSummary } from "./quality-review.ts";
import { youtubeUploadAction } from "./youtube.ts";

/** ready = current complete render whose final MP4 exists; the only playable state. */
export type QueueRenderState = "ready" | "missing" | "stale" | "not_rendered";

export type QueueProjectSummary = {
  id: string;
  title: string;
  status: string;
  current_revision: number;
  render: { state: QueueRenderState; status: string | null; revision: number | null; final_video_url: string | null };
  duration_seconds: number | null;
  width: number | null;
  height: number | null;
  poster_url: string | null;
  quality: {
    ai_review: { status: NonNullable<ProjectState["ai_review"]>["status"] } | null;
    final_review: { status: FinalQualityReview["status"]; revision: number | null; summary: { label: string } | null } | null;
  };
  youtube: { current_render: ProjectYouTube["current_render"]; upload: YouTubeUpload | null };
  /** Every platform/account publication of the project (YouTube + Instagram + TikTok). */
  publications?: ProjectPublication[];
};

export type QueueItem = { job: GenerationJob; project: QueueProjectSummary | null };

export type QueueOverview = {
  run_started_at: string | null;
  youtube: { status: YouTubeConnection["status"]; channel_title: string | null };
  /** Connected publishing accounts across all platforms (any one enables Upload). */
  publishing?: { connected_accounts: number };
  items: QueueItem[];
};

/**
 * The jobs poll is fresher than the overview for a job's own fields (progress,
 * stage, status); a job that left the queue meanwhile ("removed") is dropped.
 */
export function mergeLiveJobs(items: QueueItem[], jobs: GenerationJob[] | null): QueueItem[] {
  if (!jobs) return items;
  const live = new Map(jobs.map((job) => [job.id, job]));
  return items
    .map((item) => ({ ...item, job: live.get(item.job.id) ?? item.job }))
    .filter((item) => item.job.status !== "removed");
}

/** Which jobs exist and in which status - progress alone never changes it. */
export function queueSignature(jobs: GenerationJob[]): string {
  return jobs.map((job) => `${job.id}:${job.status}`).join("|");
}

/**
 * Called with every jobs poll result: the overview (render, review, YouTube
 * state) is re-read only when a job changed status or the job set changed, e.g.
 * a generation finished and its row becomes previewable.  The first result only
 * records the signature (the poller loads the overview on start anyway).
 */
export function overviewRefreshNeeded(previous: string | null, jobs: GenerationJob[]): { signature: string; refresh: boolean } {
  const signature = queueSignature(jobs);
  return { signature, refresh: previous !== null && previous !== signature };
}

export type QueueRowTone = "waiting" | "active" | "ok" | "muted" | "error";
export type QueueRowState = "queued" | "running" | "cancelling" | "rendered" | "completed" | "failed" | "cancelled" | "unavailable";

export function queueRowStatus(item: QueueItem): { state: QueueRowState; label: string; tone: QueueRowTone; detail: string | null } {
  const { job, project } = item;
  if (job.status === "queued") return { state: "queued", label: "Queued", tone: "waiting", detail: job.queue_position ? `Position ${job.queue_position} in the queue` : null };
  if (job.status === "running") return { state: "running", label: "Generating", tone: "active", detail: job.stage_label || null };
  if (job.status === "cancelling") return { state: "cancelling", label: "Cancelling…", tone: "muted", detail: job.stage_label || null };
  if (job.status === "failed") return { state: "failed", label: "Failed", tone: "error", detail: job.failure_message };
  if (job.status === "cancelled") return { state: "cancelled", label: "Cancelled", tone: "muted", detail: "The project is kept." };
  if (!project) return { state: "unavailable", label: "Project unavailable", tone: "muted", detail: "This project was deleted or could not be loaded." };
  switch (project.render.state) {
    case "ready": return { state: "rendered", label: "Rendered", tone: "ok", detail: null };
    case "missing": return { state: "completed", label: "Render missing", tone: "error", detail: "The final video file is not available. Open the project to render it again." };
    case "stale": return { state: "completed", label: "Render outdated", tone: "muted", detail: "The project changed after its last render. Open it to render the current revision." };
    default: return { state: "completed", label: "Not rendered", tone: "muted", detail: project.render.status === "blocked_by_research" ? "Rendering is blocked by the research check." : "Open the project to render it." };
  }
}

/** Progress is shown only where the backend reports it: a generating job. */
export function queueProgressPercent(job: GenerationJob): number | null {
  return job.status === "running" ? Math.max(0, Math.min(100, Math.round(job.progress * 100))) : null;
}

/** The canonical final MP4 (narration + mixed music), only for a finished job's current, complete render. */
export function playableSource(item: QueueItem): string | null {
  if (item.job.status !== "completed" || !item.project) return null;
  return item.project.render.state === "ready" ? item.project.render.final_video_url : null;
}

export type QueueQualityBadge = { label: "Ready" | "Needs fix" | "Review unavailable" | "Review outdated"; tone: "ok" | "attention" | "muted"; title: string | null };

const AI_REVIEW_READY = new Set(["passed", "passed_with_warnings"]);
const AI_REVIEW_FIX = new Set(["needs_fix", "failed"]);

/**
 * One compact, informational badge from the reviews the project already has:
 * the Final Video Critic's own summary (qualityReviewSummary) and the AI Review
 * status.  No second quality policy - and not a publication gate.
 */
export function queueQualityBadge(project: QueueProjectSummary | null): QueueQualityBadge | null {
  if (!project || project.render.state !== "ready") return null;
  const aiStatus = project.quality.ai_review?.status ?? null;
  const aiTitle = aiStatus ? `AI Review: ${aiStatus.replaceAll("_", " ")}` : null;
  const stored = project.quality.final_review;
  // The critic's own summary; a review without a revision is not compared.
  const review = stored ? { ...stored, revision: stored.revision ?? undefined } : null;
  const summary = review ? qualityReviewSummary(review, project.render.revision ?? undefined) : null;
  if (summary && review) {
    const title = [`Final review: ${summary.label}`, aiTitle].filter(Boolean).join(" · ");
    if (review.revision !== undefined && project.render.revision !== null && review.revision !== project.render.revision) return { label: "Review outdated", tone: "muted", title };
    if (summary.tone === "attention" || (aiStatus && AI_REVIEW_FIX.has(aiStatus))) return { label: "Needs fix", tone: "attention", title };
    if (summary.tone === "passed" || summary.tone === "repaired") return { label: "Ready", tone: "ok", title };
    return { label: "Review unavailable", tone: "muted", title };
  }
  if (aiStatus && AI_REVIEW_FIX.has(aiStatus)) return { label: "Needs fix", tone: "attention", title: aiTitle };
  if (aiStatus && AI_REVIEW_READY.has(aiStatus)) return { label: "Ready", tone: "ok", title: aiTitle };
  return { label: "Review unavailable", tone: "muted", title: aiTitle };
}

export type QueueUploadAction =
  | { kind: "upload"; label: "Upload" }
  | { kind: "connect"; label: "Connect an account" | "Reconnect YouTube" }
  | { kind: "none"; reason: string | null };

/**
 * The Results page's rule for a queue row: "Upload" opens the one unified
 * sheet (its account selector picks YouTube, Instagram or TikTok); nothing is
 * uploaded from here.  YouTube alone keeps the youtubeUploadAction rule; any
 * further connected account (another channel, Instagram, TikTok) makes Upload
 * available for a playable final video.
 */
export function queueUploadAction(overview: Pick<QueueOverview, "youtube" | "publishing">, item: QueueItem): QueueUploadAction {
  const project = item.project;
  if (!project || !playableSource(item)) return { kind: "none", reason: null };
  const connection = overview.youtube.status;
  const accounts = overview.publishing?.connected_accounts ?? (connection === "not_connected" ? 0 : 1);
  const youtube = youtubeUploadAction(connection, project.youtube.current_render, project.youtube.upload);
  if (youtube.canUpload) return { kind: "upload", label: "Upload" };
  // The default channel's own status is handled above; any further account means Upload.
  const youtubeAccounts = connection === "not_connected" ? 0 : 1;
  if (accounts > youtubeAccounts) return { kind: "upload", label: "Upload" };
  if (connection === "auth_expired") return { kind: "connect", label: "Reconnect YouTube" };
  if (accounts === 0) return { kind: "connect", label: "Connect an account" };
  return { kind: "none", reason: youtube.blockedReason };
}

/** Compact non-YouTube publication lines for a queue row (YouTube has its own line). */
export function queuePublicationLines(project: QueueProjectSummary | null): ProjectPublication[] {
  return (project?.publications ?? []).filter((item) => item.platform !== "youtube" && item.state !== "cancelled").slice(0, 3);
}

/** An upload the sheet handed to the background: the row re-reads until YouTube has answered. */
export function uploadInFlight(overview: QueueOverview | null): boolean {
  return !!overview?.items.some((item) =>
    item.project?.youtube.upload?.current?.state === "uploading"
    || (item.project?.publications ?? []).some((publication) => publication.platform !== "youtube" && ["pending", "uploading", "processing"].includes(publication.state)));
}

export type PlaybackElement = { pause: () => void };

/**
 * Only one inline queue video plays at a time: whenever one starts (poster
 * click or its own controls), every other registered player is paused.
 */
export function createExclusivePlayback() {
  const players = new Map<string, PlaybackElement>();
  return {
    register(id: string, element: PlaybackElement): () => void {
      players.set(id, element);
      return () => {
        if (players.get(id) === element) players.delete(id);
      };
    },
    playing(id: string) {
      for (const [other, element] of players) if (other !== id) element.pause();
    },
    pauseAll() {
      for (const element of players.values()) element.pause();
    },
    get size() {
      return players.size;
    },
  };
}

export type ExclusivePlayback = ReturnType<typeof createExclusivePlayback>;
