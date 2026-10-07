"use client";

import { useCallback, useEffect, useState } from "react";
import { ArrowLeft, CalendarClock, Clapperboard, ExternalLink, FolderOpen, LoaderCircle, RefreshCw } from "lucide-react";
import { ApiError, getVideo, refreshYouTubeAnalytics, syncYouTubeUpload } from "@/lib/api";
import {
  browserLocale,
  classificationLabel,
  formatDateTime,
  formatRatio,
  freshnessLine,
  sceneTitle,
  visibleSceneRows,
} from "@/lib/youtube";
import {
  DETAILED_METRICS,
  analyticsStateLabel,
  associationPairs,
  curveTicks,
  dateLine,
  detailedMetric,
  formatClock,
  humanize,
  liveStatValue,
  liveStatsNote,
  projectHref,
  removalNotice,
  retentionPolyline,
  sceneChange,
  type VideoDetail,
} from "@/lib/videos";
import { cn } from "@/lib/utils";
import { Brand } from "./brand";
import { Alert } from "./ui/alert";
import { Button } from "./ui/button";
import { DeleteVideoButton, DeleteVideoDialog } from "./delete-video-dialog";
import { ThemeToggle } from "./theme-toggle";
import { BackLink } from "./back-link";
import { StateChip, VideoThumbnail, scheduledTime } from "./video-library";
import { PageLink } from "./page-link";

const CURVE_WIDTH = 600;
const CURVE_HEIGHT = 160;

function errorText(reason: unknown, fallback: string): string {
  return reason instanceof ApiError || reason instanceof Error ? reason.message : fallback;
}

function Section({ title, source, children }: { title: string; source?: string; children: React.ReactNode }) {
  return (
    <section className="workspace-card p-4 sm:p-5" aria-label={title}>
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-sm font-semibold">{title}</h2>
        {source && <span className="mono text-[10px] uppercase tracking-[.08em] text-[var(--muted-foreground)]">{source}</span>}
      </div>
      <div className="mt-3">{children}</div>
    </section>
  );
}

function Facts({ rows }: { rows: Array<[string, React.ReactNode]> }) {
  return (
    <dl className="grid gap-x-4 gap-y-2 text-xs sm:grid-cols-2">
      {rows.map(([label, value]) => (
        <div key={label} className="min-w-0">
          <dt className="text-[var(--muted-foreground)]">{label}</dt>
          <dd className="break-words font-semibold">{value ?? "—"}</dd>
        </div>
      ))}
    </dl>
  );
}

function Tiles({ rows }: { rows: Array<[string, string]> }) {
  return (
    <dl className="grid grid-cols-2 gap-2 sm:grid-cols-4">
      {rows.map(([label, value]) => (
        <div key={label} className="cf-subtle rounded-xl border px-3 py-2">
          <dt className="text-[10px] font-bold uppercase tracking-[.08em] text-[var(--muted-foreground)]">{label}</dt>
          <dd className="mt-0.5 text-sm font-semibold">{value}</dd>
        </div>
      ))}
    </dl>
  );
}

/** Straight segments between YouTube's stored buckets; nothing smoothed or interpolated. */
function RetentionCurve({ detail }: { detail: VideoDetail }) {
  const { points, maxRatio } = retentionPolyline(detail.retention_curve, CURVE_WIDTH, CURVE_HEIGHT);
  const hundred = CURVE_HEIGHT - CURVE_HEIGHT / maxRatio;
  const ticks = curveTicks(detail.video.duration_seconds);
  return (
    <figure>
      <svg viewBox={`0 0 ${CURVE_WIDTH} ${CURVE_HEIGHT}`} className="h-40 w-full overflow-visible" role="img" aria-label={`Audience retention curve, ${detail.retention_curve.length} points`} preserveAspectRatio="none">
        <line x1={0} x2={CURVE_WIDTH} y1={hundred} y2={hundred} stroke="currentColor" strokeOpacity={0.15} strokeDasharray="4 4" vectorEffect="non-scaling-stroke" />
        <line x1={0} x2={CURVE_WIDTH} y1={CURVE_HEIGHT} y2={CURVE_HEIGHT} stroke="currentColor" strokeOpacity={0.2} vectorEffect="non-scaling-stroke" />
        <polyline points={points} fill="none" stroke="#e95528" strokeWidth={2} vectorEffect="non-scaling-stroke" />
      </svg>
      <div className="mono mt-1 flex justify-between text-[10px] text-[var(--muted-foreground)]">
        {ticks.map((tick) => <span key={tick.ratio}>{tick.label}</span>)}
      </div>
      <figcaption className="mt-1 text-[10px] text-[var(--muted-foreground)]">
        audienceWatchRatio · {detail.retention_curve.length} raw buckets from YouTube Analytics · dashed line = 100%{maxRatio > 1 ? " (replays can exceed 100%)" : ""}
      </figcaption>
    </figure>
  );
}

export function VideoDetailPage({ videoId }: { videoId: string }) {
  const locale = browserLocale();
  const [detail, setDetail] = useState<VideoDetail | null>(null);
  const [error, setError] = useState<{ text: string; missing: boolean } | null>(null);
  const [busy, setBusy] = useState<"status" | "analytics" | null>(null);
  const [notice, setNotice] = useState<{ tone: "error" | "info"; text: string } | null>(null);
  const [now, setNow] = useState(() => new Date());
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleted, setDeleted] = useState<{ tone: "success" | "warning"; text: string } | null>(null);

  const load = useCallback(async () => {
    try {
      setDetail(await getVideo(videoId));
      setError(null);
      setNow(new Date());
    } catch (reason) {
      setError({ text: errorText(reason, "This video could not be loaded."), missing: reason instanceof ApiError && reason.statusCode === 404 });
    }
  }, [videoId]);

  useEffect(() => {
    const controller = new AbortController();
    getVideo(videoId, controller.signal)
      .then((next) => { setDetail(next); setError(null); setNow(new Date()); })
      .catch((reason) => {
        if (!controller.signal.aborted) setError({ text: errorText(reason, "This video could not be loaded."), missing: reason instanceof ApiError && reason.statusCode === 404 });
      });
    return () => controller.abort();
  }, [videoId]);

  async function run(action: "status" | "analytics") {
    if (!detail) return;
    setBusy(action);
    setNotice(null);
    try {
      if (action === "status") {
        await syncYouTubeUpload(detail.video.id);
      } else {
        const result = await refreshYouTubeAnalytics(detail.video.id);
        if (result.result.status === "error") setNotice({ tone: "error", text: result.result.error?.message ?? "YouTube Analytics did not answer." });
        else if (result.result.status === "not_published") setNotice({ tone: "info", text: "Detailed analytics start after YouTube publishes the video." });
      }
      await load();
    } catch (reason) {
      setNotice({ tone: "error", text: errorText(reason, "YouTube could not be reached.") });
      await load();
    } finally {
      setBusy(null);
    }
  }

  const header = (
    <header className="sticky top-0 z-40 border-b border-[var(--border)] bg-[var(--surface)] backdrop-blur-xl">
      <div className="mx-auto flex min-h-16 max-w-[1100px] items-center justify-between gap-4 px-4 sm:px-6">
        <PageLink href="/" aria-label="ClipForge home"><Brand /></PageLink>
        <nav aria-label="Main" className="flex items-center gap-1">
          <ThemeToggle />
          <Button asChild variant="ghost" size="sm"><BackLink href="/videos"><ArrowLeft className="size-3.5" /> <Clapperboard className="size-3.5" /> Videos</BackLink></Button>
        </nav>
      </div>
    </header>
  );

  if (deleted) {
    return (
      <main className="theme-app min-h-screen bg-[var(--background)]">
        {header}
        <div className="mx-auto max-w-[1100px] px-4 pt-10 sm:px-6">
          <Alert tone={deleted.tone} size="lg" title="Deleted from ClipForge">{deleted.text}</Alert>
          <Button asChild variant="outline" size="sm" className="mt-4"><BackLink href="/videos"><ArrowLeft className="size-3.5" /> Back to Videos</BackLink></Button>
        </div>
      </main>
    );
  }

  if (!detail) {
    return (
      <main className="theme-app min-h-screen bg-[var(--background)]">
        {header}
        <div className="mx-auto max-w-[1100px] px-4 pt-10 sm:px-6">
          {error ? (
            <div role="alert" className="workspace-card p-5">
              <p className="font-semibold">{error.missing ? "Video not found" : "Could not load this video"}</p>
              <p className="mt-1 text-sm text-[var(--muted-foreground)]">{error.missing ? "It is not in the Video Library: only successful uploads appear there, and a video deleted from ClipForge is no longer listed." : error.text}</p>
              <Button asChild variant="outline" size="sm" className="mt-4"><BackLink href="/videos"><ArrowLeft className="size-3.5" /> Back to Videos</BackLink></Button>
            </div>
          ) : <p className="flex items-center gap-2 text-sm text-[var(--muted-foreground)]"><LoaderCircle className="size-4 animate-spin" /> Loading…</p>}
        </div>
      </main>
    );
  }

  const { video, upload, performance, production, publishing } = detail;
  const current = upload.current;
  const zone = current.requested.timezone ?? publishing.timezone ?? undefined;
  const freshness = freshnessLine(current, now, locale, zone);
  const project = projectHref(video);
  const metrics = performance.latest_snapshot?.metrics ?? {};
  const scenes = visibleSceneRows(performance.scene_retention);
  const opening = performance.opening_retention;
  const evidence = performance.evidence ?? [];
  const connected = video.channel.title !== null;
  const scheduled = video.state === "scheduled" && !!video.scheduled_for;

  return (
    <main className="theme-app min-h-screen bg-[var(--background)]">
      {header}
      <div className="mx-auto max-w-[1100px] space-y-4 px-4 pb-20 pt-8 sm:px-6">
        <div className="flex flex-col gap-4 sm:flex-row">
          <VideoThumbnail video={video} className="w-24 sm:w-28" />
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2"><StateChip video={video} />{video.content_type && <span className="text-[11px] text-[var(--muted-foreground)]">{humanize(video.content_type.toLowerCase())}</span>}</div>
            <h1 className="mt-2 break-words text-2xl font-semibold tracking-[-.04em]">{video.title}</h1>
            <p className={cn("mt-1 flex items-center gap-1.5 text-sm", scheduled ? "cf-text-info font-semibold" : "font-medium")} aria-label="Date">
              {scheduled && <CalendarClock className="size-4 shrink-0" />}
              {dateLine(video, (iso) => formatDateTime(iso), (iso) => scheduledTime(iso, video.schedule_timezone))}
            </p>
            <p className="mt-1 text-xs text-[var(--muted-foreground)]">
              {video.channel.title ?? video.channel.id}
              {video.duration_seconds !== null ? ` · ${formatClock(video.duration_seconds)}` : ""}
            </p>
            <p className="mono mt-0.5 text-[10px] text-[var(--muted-foreground)]" title="YouTube video ID">YouTube ID {video.youtube_video_id}</p>
            {video.project.available ? (
              <p className="cf-text-success mt-1 text-xs font-semibold">Project available</p>
            ) : (
              <p className="mt-1 text-xs"><span className="font-semibold">Project deleted</span> <span className="text-[var(--muted-foreground)]">· Learning data retained{video.project.archived_at ? ` since ${formatDateTime(video.project.archived_at)}` : ""}</span></p>
            )}
            <div className="mt-3 flex flex-wrap gap-2 text-xs">
              {project && <Button asChild variant="outline" size="sm"><PageLink href={project}><FolderOpen className="size-3.5" /> Open project</PageLink></Button>}
              {video.youtube_actions.available ? (
                <>
                  {video.watch_url && <Button asChild variant="outline" size="sm"><a href={video.shorts_url ?? video.watch_url} target="_blank" rel="noreferrer"><ExternalLink className="size-3.5" /> Open on YouTube</a></Button>}
                  {video.studio_url && <Button asChild variant="outline" size="sm"><a href={video.studio_url} target="_blank" rel="noreferrer"><ExternalLink className="size-3.5" /> Open in YouTube Studio</a></Button>}
                </>
              ) : (
                <>
                  <Button variant="outline" size="sm" disabled aria-describedby="youtube-actions-reason"><ExternalLink className="size-3.5" /> Open on YouTube</Button>
                  <Button variant="outline" size="sm" disabled aria-describedby="youtube-actions-reason"><ExternalLink className="size-3.5" /> Open in YouTube Studio</Button>
                </>
              )}
              <Button variant="ghost" size="sm" onClick={() => void run("status")} disabled={busy !== null || !connected} title={connected ? undefined : "Connect this video's channel in Settings to refresh"}>
                {busy === "status" ? <LoaderCircle className="size-3.5 animate-spin" /> : <RefreshCw className="size-3.5" />} Refresh status
              </Button>
              <Button variant="ghost" size="sm" onClick={() => void run("analytics")} disabled={busy !== null || !connected || current.state === "deleted"} title={connected ? undefined : "Connect this video's channel in Settings to refresh"}>
                {busy === "analytics" ? <LoaderCircle className="size-3.5 animate-spin" /> : <RefreshCw className="size-3.5" />} Refresh analytics
              </Button>
              <DeleteVideoButton video={video} size="page" onClick={() => setConfirmDelete(true)} />
            </div>
            {!video.youtube_actions.available && <p id="youtube-actions-reason" className="mt-2 text-xs text-[var(--muted-foreground)]">{video.youtube_actions.reason}</p>}
            {notice && <Alert tone={notice.tone} size="sm" className="mt-2">{notice.text}</Alert>}
          </div>
        </div>

        <div className="grid gap-4 lg:grid-cols-2">
          <Section title="Status" source="YouTube">
            {current.stale && (
              <Alert tone="warning" size="sm" className="mb-3">
                {current.stale_reason === "refresh_failed" ? `Could not refresh the YouTube status${current.refresh_error ? `: ${current.refresh_error.message}` : ""}. Showing the last confirmed state.` : current.stale_reason === "never_checked" ? "Not checked with YouTube yet." : "The status may be outdated."}
              </Alert>
            )}
            <Facts rows={[
              ["Current YouTube state", current.label],
              ["Scheduled time", current.scheduled_for ? scheduledTime(current.scheduled_for, video.schedule_timezone) : "—"],
              ["Published time", current.published_at ? formatDateTime(current.published_at) : "—"],
              ["Last remote status check", freshness ?? "—"],
            ]} />
          </Section>

          <Section title="Live stats" source="YouTube Data API">
            {video.live_stats_state === "available" && video.live_stats ? (
              <>
                <Tiles rows={[["Views", liveStatValue(video, "views")], ["Likes", liveStatValue(video, "likes")], ["Comments", liveStatValue(video, "comments")]]} />
                <p className="mt-2 text-[10px] text-[var(--muted-foreground)]">{current.state === "deleted" ? "Last known values · " : ""}As of {formatDateTime(video.live_stats.checked_at)}</p>
              </>
            ) : video.live_stats_state === "not_published" ? (
              <p className="text-xs text-[var(--muted-foreground)]"><span className="font-semibold text-[var(--foreground)]">{liveStatsNote("not_published")}.</span> YouTube reports no audience numbers before the video is public.</p>
            ) : <p className="text-xs text-[var(--muted-foreground)]">— {liveStatsNote("not_reported")}.</p>}
          </Section>
        </div>

        <Section title="Detailed analytics" source="YouTube Analytics API">
          <p className="mb-3 text-xs"><span className="font-semibold">{analyticsStateLabel(performance.analytics_state ?? "not_published")}</span>
            {performance.latest_snapshot ? <span className="text-[var(--muted-foreground)]"> · {formatDateTime(performance.latest_snapshot.fetched_at)}{performance.latest_snapshot.published_age_hours != null ? ` · ${Math.round(performance.latest_snapshot.published_age_hours)} h after publishing` : ""}</span> : null}
          </p>
          {performance.analytics_error && <Alert tone="error" className="mb-2">{performance.analytics_error.message}</Alert>}
          <Tiles rows={DETAILED_METRICS.map(([name, label]) => [label, detailedMetric(name, metrics[name], performance.analytics_state)])} />
        </Section>

        <Section title="Retention" source="YouTube Analytics API">
          {opening?.status === "ok" ? (
            <p className="text-xs"><span className="font-semibold">Opening retention</span>{" "}
              {opening.points.filter((item) => item.audience_watch_ratio != null).map((item) => <span key={item.target_second} className="mr-3">~{item.target_second}s: <strong>{formatRatio(item.audience_watch_ratio)}</strong></span>)}
            </p>
          ) : <p className="text-xs text-[var(--muted-foreground)]">Opening retention: {performance.analytics_state === "processing" || performance.analytics_state === "partial" ? "Processing" : "—"}</p>}
          {detail.retention_curve.length > 1 ? <div className="mt-3"><RetentionCurve detail={detail} /></div> : <p className="mt-2 text-xs text-[var(--muted-foreground)]">No audience retention curve yet.</p>}
          {scenes.length > 0 && (
            <div className="mt-4">
              <p className="text-xs font-semibold">Retention by scene</p>
              <ol className="mt-2 grid gap-1.5 text-xs sm:grid-cols-2">
                {scenes.map((scene) => {
                  const change = sceneChange(scene);
                  return (
                    <li key={scene.scene_id} className={cn("rounded-xl border px-3 py-2", scene.notable_drop ? "cf-tone-warning" : "cf-subtle")}>
                      <div className="flex items-center justify-between gap-2"><span className="font-semibold">{sceneTitle(scene)}</span><span className="mono text-[10px]">{formatClock(scene.start)}–{formatClock(scene.end)}</span></div>
                      <p className="mt-0.5">{change.range} <strong className="ml-1">{change.delta}</strong></p>
                    </li>
                  );
                })}
              </ol>
            </div>
          )}
          {detail.major_drops.length > 0 && (
            <p className="mt-3 text-[11px] text-[var(--muted-foreground)]">
              Major drops (existing scene thresholds): {detail.major_drops.map((scene) => `${sceneTitle(scene)} ${sceneChange(scene).delta}`).join(" · ")}. A drop describes where viewers left; it does not by itself make a scene bad.
            </p>
          )}
        </Section>

        <div className="grid gap-4 lg:grid-cols-2">
          <Section title="Channel comparison">
            {performance.classification ? (
              <>
                <p className="text-sm font-semibold">{classificationLabel(performance.classification.label)}</p>
                <p className="mt-1 text-[11px] text-[var(--muted-foreground)]">
                  {performance.baseline ? `Sample size: n=${performance.baseline.sample_size} comparable Shorts of your channel (minimum ${performance.baseline.min_sample}).` : ""}
                  {/* Other reasons (e.g. not confirmed as a Short); the sample-size one is the line above. */}
                  {performance.classification.reason && performance.baseline?.sufficient ? ` ${performance.classification.reason}.` : ""}
                </p>
              </>
            ) : <p className="text-xs text-[var(--muted-foreground)]">Available once detailed analytics exist.</p>}
          </Section>

          <Section title="Learning observations">
            {evidence.length ? (
              <ul className="space-y-3 text-xs">
                {evidence.map((item, index) => (
                  <li key={index}>
                    <p>{item.observation}</p>
                    {associationPairs(item.associated_production_data).length > 0 && (
                      <p className="mt-1 text-[11px] text-[var(--muted-foreground)]">Associated with: {associationPairs(item.associated_production_data).map(([key, value]) => `${key}: ${value}`).join(" · ")}</p>
                    )}
                    <p className="mono mt-1 text-[10px] text-[var(--muted-foreground)]">Confidence: {humanize(item.confidence)} · Sample size: n={item.sample_size}</p>
                  </li>
                ))}
                <li className="text-[10px] text-[var(--muted-foreground)]">Associations only; they do not show that a production choice caused the result.</li>
              </ul>
            ) : <p className="text-xs text-[var(--muted-foreground)]">No observations yet.</p>}
          </Section>
        </div>

        <div className="grid gap-4 lg:grid-cols-2">
          <Section title="Production context">
            {production.available ? (
              <Facts rows={[
                ["Original prompt", production.prompt ?? "—"],
                ["Topic", production.topic ?? "—"],
                ["Format", humanize(production.format)],
                ["Duration", formatClock(production.duration_seconds)],
                ["Scenes", production.scene_count ?? "—"],
                ["Verbal hook strategy", humanize(production.hook_strategy)],
                ["Final verbal hook", production.verbal_hook ?? "—"],
                ["Visual hook intent", production.visual_hook ? [production.visual_hook.subject, humanize(production.visual_hook.visual_strategy), production.visual_hook.visual_goal].filter((value) => value && value !== "—").join(" · ") || "—" : "—"],
                ["On-screen hook", production.on_screen_hook ?? "—"],
                ["Answer / reveal", production.answer_reveal_seconds !== null ? `at ${formatClock(production.answer_reveal_seconds)}` : "—"],
                ["Final Critic issues", production.critic.issue_count ?? "—"],
                ["Successful repairs", production.critic.repairs_successful !== null ? `${production.critic.repairs_successful} of ${production.critic.repairs_attempted ?? 0}` : "—"],
                ["Unresolved issues", production.critic.unresolved_issue_count ?? "—"],
              ]} />
            ) : (
              <Facts rows={[["Original prompt", production.prompt ?? "—"], ["Topic", production.topic ?? "—"], ["Production record", "Not available for this upload"]]} />
            )}
          </Section>

          <Section title="Publishing">
            <Facts rows={[
              ["Schedule provenance", publishing.provenance],
              ["Smart Scheduler selected it", publishing.smart_scheduler_selected ? "Yes" : "No"],
              ["Selected slot", publishing.slot_time ?? publishing.local_time ?? "—"],
              ["Timezone", publishing.timezone ?? "—"],
              ["Requested time", publishing.requested_publish_at ? formatDateTime(publishing.requested_publish_at) : "—"],
              ["Requested visibility", humanize(publishing.requested_visibility)],
              ["Uploaded", publishing.uploaded_at ? formatDateTime(publishing.uploaded_at) : "—"],
            ]} />
          </Section>
        </div>
      </div>
      {confirmDelete && (
        <DeleteVideoDialog
          video={video}
          onCancel={() => setConfirmDelete(false)}
          onDeleted={(result) => { setConfirmDelete(false); setDeleted(removalNotice(result, video.title)); }}
        />
      )}
    </main>
  );
}
