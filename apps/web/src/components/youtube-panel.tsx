"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { CalendarClock, ExternalLink, LoaderCircle, RefreshCw, Upload } from "lucide-react";
import {
  ApiError,
  getProjectYouTube,
  getPublishTargets,
  refreshYouTubeAnalytics,
  retryYouTubeThumbnail,
  retryYouTubeUpload,
  scheduleYouTubeUpload,
  setYouTubeAudience,
  syncYouTubeUpload,
} from "@/lib/api";
import type { PublishTargets } from "@/lib/publishing";
import type { Project } from "@/lib/types";
import {
  PERFORMANCE_METRICS,
  browserLocale,
  analyticsReadiness,
  classificationLabel,
  currentHeadline,
  detectTimeZone,
  freshnessLine,
  formatDateTime,
  formatDelta,
  formatMetric,
  formatRatio,
  formatSeconds,
  lifecycleLabel,
  performanceHeadline,
  sceneTitle,
  scheduleLine,
  statusRows,
  visibleSceneRows,
  youtubeUploadAction,
  type PerformanceReport,
  type ProjectYouTube,
  type ScheduleChoice,
  type ScheduleResolution,
  type YouTubeUpload,
} from "@/lib/youtube";
import { cn } from "@/lib/utils";
import { Button } from "./ui/button";
import { SocialPublications } from "./social-publications";
import { Alert } from "./ui/alert";
import { UploadSheet } from "./upload-sheet";
import { ScheduleFields } from "./youtube-schedule-fields";
import { SectionLink } from "./section-link";

type Notice = { tone: "error" | "info" | "success"; text: string };

const MAX_STATUS_POLLS = 60;
const STUDIO_ONLY_HINT = "Chapters, comments, Shorts remixing, featured places, age restriction and monetization are set in YouTube Studio.";

function errorText(reason: unknown, fallback: string): string {
  return reason instanceof ApiError || reason instanceof Error ? reason.message : fallback;
}

export function YouTubePanel({ project, disabled, publishOpen, onPublishOpenChange }: {
  project: Project;
  disabled: boolean;
  publishOpen: boolean;
  onPublishOpenChange: (open: boolean) => void;
}) {
  const locale = browserLocale();
  const [data, setData] = useState<ProjectYouTube | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [scheduling, setScheduling] = useState(false);
  const [schedule, setSchedule] = useState<ScheduleChoice>(() => ({ date: "", time: "", timezone: detectTimeZone() }));
  const [resolution, setResolution] = useState<ScheduleResolution | null>(null);
  // Instagram/TikTok accounts make Upload available even without YouTube.
  const [targets, setTargets] = useState<PublishTargets | null>(null);
  const [publicationsVersion, setPublicationsVersion] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    getPublishTargets(project.id, controller.signal).then(setTargets).catch(() => undefined);
    return () => controller.abort();
  }, [project.id, project.current_revision, publicationsVersion]);

  const load = useCallback(async () => {
    try {
      setData(await getProjectYouTube(project.id));
      setLoadError(null);
    } catch (reason) {
      setLoadError(errorText(reason, "YouTube status could not be loaded."));
    }
  }, [project.id]);

  useEffect(() => {
    const controller = new AbortController();
    getProjectYouTube(project.id, controller.signal)
      .then((next) => { setData(next); setLoadError(null); })
      .catch((reason) => { if (!controller.signal.aborted) setLoadError(errorText(reason, "YouTube status could not be loaded.")); });
    return () => controller.abort();
  }, [project.id, project.current_revision]);

  const focus = data?.uploads.find((item) => item.id === data.focus_upload_id) ?? null;
  // The backend decides the cadence (tight only around upload / publish time)
  // and gates every YouTube call by freshness; the page never polls forever.
  const nextCheck = focus?.next_status_check_in_seconds ?? null;
  const statusPolls = useRef(0);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    statusPolls.current = 0;
  }, [project.id]);

  useEffect(() => {
    if (nextCheck === null) return;
    if (nextCheck > 5 && statusPolls.current >= MAX_STATUS_POLLS) return;
    const timer = window.setTimeout(() => {
      if (nextCheck > 5) statusPolls.current += 1;
      void load();
    }, nextCheck * 1000);
    return () => window.clearTimeout(timer);
  }, [nextCheck, data, load]);

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 30_000);
    return () => window.clearInterval(timer);
  }, []);

  async function run(action: string, work: () => Promise<unknown>, success?: string) {
    setBusy(action);
    setNotice(null);
    try {
      await work();
      if (success) setNotice({ tone: "success", text: success });
    } catch (reason) {
      setNotice({ tone: "error", text: errorText(reason, "YouTube could not complete this request.") });
    } finally {
      setBusy(null);
      await load();
    }
  }

  if (!data) {
    return (
      <section className="workspace-card mt-8 p-5" aria-label="YouTube" aria-busy={!loadError}>
        <h2 className="font-semibold">YouTube</h2>
        <p className="mt-2 text-sm text-[var(--muted-foreground)]">{loadError ?? "Loading YouTube status…"}</p>
      </section>
    );
  }

  const { connection, current_render: current } = data;
  const locked = disabled || busy !== null;
  const connected = connection.status === "connected";
  const others = data.uploads.filter((item) => item.id !== focus?.id && item.youtube_video_id);
  const { canUpload: canUploadYouTube, newRevision, blockedReason: uploadBlockedReason } = youtubeUploadAction(connection.status, current, focus);
  const connectedTargets = (targets?.targets ?? []).filter((item) => item.status === "connected");
  const otherTargets = connectedTargets.filter((item) => item.platform !== "youtube" || item.external_account_id !== connection.channel_id);
  const renderReady = current.uploadable || current.code === "already_uploaded";
  // "Upload" opens the one sheet; its account selector decides the platform/account.
  const canUpload = canUploadYouTube || (renderReady && otherTargets.length > 0);

  return (
    <section className="workspace-card mt-8 p-5" aria-label="YouTube" id="youtube-publishing">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 className="font-semibold">Publishing</h2>
          <p className="mt-1 text-xs text-[var(--muted-foreground)]">YouTube{" · "}
            {connection.status === "not_connected" && "Not connected"}
            {connection.status === "auth_expired" && "YouTube sign-in expired — reconnect to continue."}
            {connected && <>Connected: <strong className="text-[var(--foreground)]">{connection.channel_title}</strong></>}
          </p>
        </div>
        {!connected && !canUpload && (
          <Button asChild size="sm" variant="accent">
            <SectionLink href="/settings/integrations#youtube">{connection.status === "auth_expired" ? "Reconnect YouTube" : "Connect an account"}</SectionLink>
          </Button>
        )}
        {canUpload && (
          <Button size="sm" variant="accent" disabled={locked} onClick={() => onPublishOpenChange(true)}>
            <Upload className="size-3.5" /> Upload
          </Button>
        )}
      </div>

      {uploadBlockedReason && <p className="mt-3 text-xs text-[var(--muted-foreground)]">{uploadBlockedReason}</p>}
      {newRevision && <p className="mt-3 text-xs text-[var(--muted-foreground)]">This revision (render v{current.render_revision}) is not on YouTube yet. Earlier revisions keep their own videos.</p>}

      {focus && (
        <UploadCard
          upload={focus}
          locale={locale}
          now={now}
          locked={locked}
          busy={busy}
          scheduling={scheduling}
          schedule={schedule}
          resolution={resolution}
          onSchedule={setSchedule}
          onResolved={setResolution}
          onScheduleOpen={() => { setSchedule((value) => ({ ...value, timezone: focus.schedule.timezone ?? value.timezone })); setScheduling(true); }}
          onScheduleCancel={() => setScheduling(false)}
          onScheduleSave={() => void run("schedule", async () => { await scheduleYouTubeUpload(focus.id, schedule); setScheduling(false); }, "Schedule saved on YouTube.")}
          onSync={() => void run("sync", () => syncYouTubeUpload(focus.id))}
          onRetry={() => void run("retry", () => retryYouTubeUpload(focus.id))}
          onRetryThumbnail={() => void run("thumbnail", () => retryYouTubeThumbnail(focus.id), "Thumbnail applied.")}
          onAudience={(value) => void run("audience", () => setYouTubeAudience(focus.id, value), "Audience answer saved on YouTube.")}
          onRefreshAnalytics={() => void run("analytics", async () => {
            const response = await refreshYouTubeAnalytics(focus.id);
            if (response.result.status === "error" && response.result.error) throw new Error(response.result.error.message);
          })}
          onReupload={() => onPublishOpenChange(true)}
        />
      )}

      {notice && (
        <Alert tone={notice.tone === "error" ? "error" : notice.tone === "info" ? "info" : "success"} className="mt-3">{notice.text}</Alert>
      )}

      {others.length > 0 && (
        <p className="mt-3 text-[11px] text-[var(--muted-foreground)]">
          Other revisions: {others.map((item) => `render v${item.render_revision} → ${item.youtube_video_id} (${lifecycleLabel(item)})`).join(" · ")}
        </p>
      )}

      <SocialPublications projectId={project.id} version={publicationsVersion} onChanged={() => setPublicationsVersion((value) => value + 1)} />

      <PerformanceSection report={data.performance} />

      {publishOpen && (
        <UploadSheet
          project={project}
          onClose={() => { onPublishOpenChange(false); void load(); setPublicationsVersion((value) => value + 1); }}
          onUploaded={() => { onPublishOpenChange(false); void load(); setPublicationsVersion((value) => value + 1); }}
        />
      )}
    </section>
  );
}

function UploadCard({ upload, locale, now, locked, busy, scheduling, schedule, resolution, onSchedule, onResolved, onScheduleOpen, onScheduleCancel, onScheduleSave, onSync, onRetry, onRetryThumbnail, onAudience, onRefreshAnalytics, onReupload }: {
  upload: YouTubeUpload; locale: string; now: number; locked: boolean; busy: string | null; scheduling: boolean; schedule: ScheduleChoice; resolution: ScheduleResolution | null;
  onSchedule: (value: ScheduleChoice) => void; onResolved: (value: ScheduleResolution | null) => void; onScheduleOpen: () => void; onScheduleCancel: () => void; onScheduleSave: () => void;
  onSync: () => void; onRetry: () => void; onRetryThumbnail: () => void; onAudience: (value: boolean) => void; onRefreshAnalytics: () => void; onReupload: () => void;
}) {
  const current = upload.current;
  const live = current.state === "published" || current.state === "unlisted";
  const exists = !!upload.youtube_video_id && !upload.deleted_on_youtube;
  const canChangeSchedule = exists && ["private", "scheduled", "publish_pending"].includes(current.state);
  const retryable = current.state === "upload_failed" && upload.error?.code !== "session_expired_unknown_outcome";
  const answered = (upload.audience.confirmed_by_youtube ?? upload.audience.made_for_kids) !== null;
  const spin = (name: string) => (busy === name ? <LoaderCircle className="size-3.5 animate-spin" /> : null);
  const zone = current.requested.timezone ?? "UTC";
  const nowDate = new Date(now);
  const freshness = exists ? freshnessLine(current, nowDate, locale) : null;
  return (
    <div className="cf-subtle mt-4 rounded-[.85rem] border p-3 text-xs">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0" aria-live="polite">
          <p className={cn("text-lg font-semibold tracking-[-.02em]", live && "cf-text-success", current.state === "publish_pending" && "cf-text-warning", ["rejected", "processing_failed", "deleted", "upload_failed"].includes(current.state) && "cf-text-error")}>
            {current.state === "uploading" ? lifecycleLabel(upload) : currentHeadline(current, nowDate, locale)}
          </p>
          {current.state === "scheduled" && current.scheduled_for && (
            <p className="mt-0.5 text-sm font-semibold">{scheduleLine(current.scheduled_for, zone, locale)} <span className="font-normal text-[var(--muted-foreground)]">· {zone}</span></p>
          )}
          {freshness && <p className={cn("mt-0.5", current.stale ? "cf-text-warning" : "text-[var(--muted-foreground)]")}>{freshness}</p>}
          <p className="mono mt-0.5 truncate text-[10px] text-[var(--muted-foreground)]">
            {upload.youtube_video_id ? `Video ${upload.youtube_video_id}` : "No video yet"} · render v{upload.render_revision}{upload.content_type ? ` · ${upload.content_type}` : ""}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-1">
          {exists && <button type="button" className="interactive-text" disabled={locked} onClick={onSync}>{spin("sync") ?? <RefreshCw className="size-3.5" />} Refresh YouTube status</button>}
          {exists && <a href={live ? upload.shorts_url ?? "#" : upload.watch_url ?? "#"} target="_blank" rel="noreferrer" className="interactive-text"><ExternalLink className="size-3.5" /> Open on YouTube</a>}
          {exists && <a href={upload.studio_url ?? "#"} target="_blank" rel="noreferrer" className="interactive-text"><ExternalLink className="size-3.5" /> Open in Studio</a>}
          {exists && upload.thumbnail.status === "failed" && <button type="button" className="interactive-text" disabled={locked} onClick={onRetryThumbnail}>{spin("thumbnail") ?? <RefreshCw className="size-3.5" />} Retry thumbnail</button>}
          {canChangeSchedule && !scheduling && <button type="button" className="interactive-text" disabled={locked} onClick={onScheduleOpen}><CalendarClock className="size-3.5" /> {current.state === "private" ? "Schedule" : "Change schedule"}</button>}
          {live && <button type="button" className="interactive-text" disabled={locked} onClick={onRefreshAnalytics}>{spin("analytics") ?? <RefreshCw className="size-3.5" />} Refresh analytics</button>}
          {retryable && <button type="button" className="interactive-text" disabled={locked} onClick={onRetry}>{spin("retry") ?? <RefreshCw className="size-3.5" />} Retry upload</button>}
        </div>
      </div>

      {current.state === "publish_pending" && (
        <Alert tone="warning" className="mt-2">YouTube still reports this video as private although the scheduled time has passed. ClipForge checks again for a while; you can also refresh or open YouTube Studio.</Alert>
      )}
      {current.refresh_error && current.stale && <p className="cf-text-warning mt-2">Could not refresh YouTube status: {current.refresh_error.message}</p>}

      {live && current.live_stats && (
        <div className="mt-3" aria-label="Live stats">
          <p className="text-[10px] font-bold uppercase tracking-[.08em] text-[var(--muted-foreground)]">Live stats</p>
          <dl className="mt-1 grid grid-cols-3 gap-2">
            {([["Views", current.live_stats.views], ["Likes", current.live_stats.likes], ["Comments", current.live_stats.comments]] as const).map(([label, value]) => (
              <div key={label} className="rounded-lg border border-[var(--border)] bg-[var(--surface-elevated)] px-3 py-2">
                <dt className="text-[10px] text-[var(--muted-foreground)]">{label}</dt>
                <dd className="text-base font-semibold">{value === null ? "hidden" : value.toLocaleString(locale)}</dd>
              </div>
            ))}
          </dl>
        </div>
      )}

      {upload.lifecycle === "uploading" && upload.progress !== null && (
        <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-black/10" role="progressbar" aria-valuenow={Math.round(upload.progress * 100)} aria-valuemin={0} aria-valuemax={100}>
          <div className="h-full bg-[#ff6838] transition-all" style={{ width: `${Math.round(upload.progress * 100)}%` }} />
        </div>
      )}

      <dl className="mt-3 grid gap-x-4 gap-y-1.5 sm:grid-cols-2">
        {statusRows(upload, locale).map((row) => (
          <div key={row.label} className="flex min-w-0 gap-2">
            <dt className="w-20 shrink-0 text-[var(--muted-foreground)]">{row.label}</dt>
            <dd className={cn("min-w-0 font-semibold", row.tone === "error" && "cf-text-error", row.tone === "warn" && "cf-text-warning", row.tone === "ok" && "cf-text-success")}>{row.value}</dd>
          </div>
        ))}
      </dl>

      {upload.thumbnail.status === "failed" && upload.thumbnail.failure_reason && (
        <Alert tone="error" className="mt-2">Video uploaded successfully. Thumbnail could not be applied: {upload.thumbnail.failure_reason}</Alert>
      )}
      {upload.visibility_restricted && <Alert tone="warning" className="mt-2">YouTube kept this video private because the Google API project has not passed YouTube&apos;s audit yet.</Alert>}
      {upload.error && upload.lifecycle !== "published" && (
        <Alert tone="error" className="mt-2">{upload.error.message}</Alert>
      )}
      {exists && !answered && (
        <Alert tone="warning" role="none" className="mt-3" title="YouTube still needs the audience answer for this video.">
          <div className="mt-2 flex flex-wrap gap-2">
            <Button size="sm" variant="outline" disabled={locked} onClick={() => onAudience(false)}>{spin("audience")} No, it&apos;s not made for kids</Button>
            <Button size="sm" variant="outline" disabled={locked} onClick={() => onAudience(true)}>Yes, it&apos;s made for kids</Button>
          </div>
        </Alert>
      )}
      {upload.can_reupload && upload.is_active_mapping && (
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <span className="text-[var(--muted-foreground)]">YouTube no longer has a usable copy of this render.</span>
          <Button size="sm" variant="outline" disabled={locked} onClick={onReupload}>Upload again</Button>
        </div>
      )}
      {scheduling && canChangeSchedule && (
        <form className="mt-3 space-y-2" onSubmit={(event) => { event.preventDefault(); onScheduleSave(); }}>
          <ScheduleFields value={schedule} onChange={onSchedule} locale={locale} onResolved={onResolved} />
          <div className="flex justify-end gap-2">
            <Button type="button" size="sm" variant="ghost" onClick={onScheduleCancel}>Cancel</Button>
            <Button type="submit" size="sm" variant="accent" disabled={locked || resolution?.status !== "ok"}>{spin("schedule") ?? <CalendarClock className="size-3.5" />} Save schedule</Button>
          </div>
        </form>
      )}
      {exists && (
        <p className="mt-3 border-t border-black/8 pt-2 text-[11px] text-[var(--muted-foreground)]">
          <strong className="text-[var(--foreground)]">Additional YouTube Studio settings.</strong> {STUDIO_ONLY_HINT} <a href={upload.studio_url ?? "#"} target="_blank" rel="noreferrer" className="underline">Open in YouTube Studio</a>
        </p>
      )}
      {live && <p className="mt-2 text-[var(--muted-foreground)]">Last analytics sync: {formatDateTime(upload.last_analytics_sync_at)}</p>}
      {(current.requested.publish_at || current.requested.history.length > 0) && (
        <details className="mt-2 text-[11px] text-[var(--muted-foreground)]">
          <summary className="cursor-pointer">Requested schedule (history)</summary>
          <ul className="mt-1 space-y-0.5">
            {current.requested.publish_at && <li>Requested: {scheduleLine(current.requested.publish_at, zone, locale)} · {zone}</li>}
            {current.requested.history.map((item) => <li key={item.replaced_at}>Earlier request: {scheduleLine(item.publish_at, item.timezone ?? "UTC", locale)} · {item.timezone ?? "UTC"}</li>)}
            {current.published_at && current.published_time_source && <li>Published per YouTube: {scheduleLine(current.published_at, zone, locale)} ({current.published_time_source === "youtube_snippet_published_at" ? "YouTube publication time" : "first seen public by ClipForge"})</li>}
          </ul>
        </details>
      )}
    </div>
  );
}

export function PerformanceSection({ report }: { report: PerformanceReport }) {
  const metrics = report.latest_snapshot?.metrics ?? {};
  const scenes = visibleSceneRows(report.scene_retention);
  const opening = report.opening_retention;
  return (
    <div className="mt-5 border-t border-black/8 pt-4" aria-label="Performance">
      <h3 className="text-sm font-semibold">Performance</h3>
      {report.status !== "ready" && !analyticsReadiness(report.analytics_state) && <p className="mt-1 text-xs text-[var(--muted-foreground)]">{performanceHeadline(report)}</p>}
      {(() => {
        const readiness = analyticsReadiness(report.analytics_state);
        return readiness && report.analytics_state !== "available" ? (
          <div className="mt-2 rounded-xl border border-[var(--border)] px-3 py-2 text-xs" aria-label="Detailed analytics">
            <p className="font-semibold">Detailed analytics · {readiness.title}</p>
            <p className="mt-0.5 text-[var(--muted-foreground)]">{readiness.body}</p>
          </div>
        ) : null;
      })()}
      {report.analytics_error && <Alert tone="error" className="mt-2">Analytics: {report.analytics_error.message}</Alert>}
      {report.status === "ready" && (
        <>
          <p className="mt-1 text-[11px] text-[var(--muted-foreground)]">
            {report.latest_snapshot ? `YouTube Analytics · ${formatDateTime(report.latest_snapshot.fetched_at)}` : "Retention only"}
            {report.latest_snapshot?.published_age_hours != null ? ` · ${Math.round(report.latest_snapshot.published_age_hours)} h after publishing` : ""}
          </p>
          <dl className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-4">
            {PERFORMANCE_METRICS.map(([name, label]) => (
              <div key={name} className="cf-subtle rounded-xl border px-3 py-2">
                <dt className="text-[10px] font-bold uppercase tracking-[.08em] text-[var(--muted-foreground)]">{label}</dt>
                <dd className="mt-0.5 text-sm font-semibold">{formatMetric(name, metrics[name])}</dd>
              </div>
            ))}
          </dl>

          {opening?.status === "ok" && (
            <div className="mt-4 text-xs">
              <p className="font-semibold">Opening retention</p>
              <p className="mt-1 flex flex-wrap gap-x-4 gap-y-1">
                {opening.points.filter((item) => item.audience_watch_ratio != null).map((item) => (
                  <span key={item.target_second}>~{item.target_second}s: <strong>{formatRatio(item.audience_watch_ratio)}</strong></span>
                ))}
              </p>
              {opening.hook.strategy && <p className="mt-1 text-[10px] text-[var(--muted-foreground)]">Hook: {opening.hook.strategy.replaceAll("_", " ")}{opening.hook.verbal_hook ? ` — “${opening.hook.verbal_hook}”` : ""}</p>}
            </div>
          )}

          {scenes.length > 0 && (
            <div className="mt-4 text-xs">
              <p className="font-semibold">Retention by scene</p>
              <ol className="mt-2 grid gap-1.5 sm:grid-cols-2">
                {scenes.map((scene) => (
                  <li key={scene.scene_id} className={cn("rounded-xl border px-3 py-2", scene.notable_drop ? "cf-tone-warning" : "cf-subtle")}>
                    <div className="flex items-center justify-between gap-2"><span className="font-semibold">{sceneTitle(scene)}</span><span className="mono text-[10px]">{formatSeconds(scene.start)}–{formatSeconds(scene.end)}</span></div>
                    <p className="mt-0.5">Retention: {formatRatio(scene.retention_entering)} → {formatRatio(scene.retention_leaving)} <strong className="ml-1">{formatDelta(scene.retention_delta)}</strong>{scene.notable_drop ? " · notable drop" : ""}</p>
                  </li>
                ))}
              </ol>
            </div>
          )}

          {report.classification && (
            <p className="mt-4 text-xs"><span className="font-semibold">Channel comparison:</span> {classificationLabel(report.classification.label)}{report.baseline ? ` (n=${report.baseline.sample_size}, needs ${report.baseline.min_sample})` : ""}</p>
          )}
          {(report.evidence ?? []).length > 0 && (
            <ul className="mt-2 space-y-1 text-[11px] text-[var(--muted-foreground)]">
              {(report.evidence ?? []).map((item, index) => <li key={index}>{item.observation} <span className="mono">[{item.confidence} confidence · n={item.sample_size}]</span></li>)}
            </ul>
          )}
        </>
      )}
    </div>
  );
}
