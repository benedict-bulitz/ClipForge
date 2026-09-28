"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { AlertTriangle, CalendarClock, ExternalLink, LoaderCircle, RefreshCw, Upload } from "lucide-react";
import {
  ApiError,
  getProjectYouTube,
  refreshYouTubeAnalytics,
  retryYouTubeUpload,
  scheduleYouTubeUpload,
  syncYouTubeUpload,
  uploadProjectToYouTube,
} from "@/lib/api";
import type { Project } from "@/lib/types";
import {
  PERFORMANCE_METRICS,
  classificationLabel,
  formatDateTime,
  formatDelta,
  formatMetric,
  formatRatio,
  formatSeconds,
  lifecycleLabel,
  localDateTimeToIso,
  performanceHeadline,
  sceneTitle,
  visibleSceneRows,
  type PerformanceReport,
  type ProjectYouTube,
  type YouTubeUpload,
} from "@/lib/youtube";
import { cn } from "@/lib/utils";
import { Button } from "./ui/button";

type Notice = { tone: "error" | "info" | "success"; text: string };

function errorText(reason: unknown, fallback: string): string {
  return reason instanceof ApiError || reason instanceof Error ? reason.message : fallback;
}

export function YouTubePanel({ project, disabled }: { project: Project; disabled: boolean }) {
  const [data, setData] = useState<ProjectYouTube | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [scheduling, setScheduling] = useState(false);
  const [date, setDate] = useState("");
  const [time, setTime] = useState("");
  const [confirmReupload, setConfirmReupload] = useState(false);

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
  const uploading = focus?.lifecycle === "uploading";

  useEffect(() => {
    if (!uploading) return;
    const timer = window.setInterval(() => void load(), 2500);
    return () => window.clearInterval(timer);
  }, [uploading, load]);

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

  function schedule(upload: YouTubeUpload) {
    const iso = localDateTimeToIso(date, time);
    if (!iso) {
      setNotice({ tone: "error", text: "Choose a date and a time." });
      return;
    }
    void run("schedule", async () => {
      await scheduleYouTubeUpload(upload.id, iso);
      setScheduling(false);
    }, "Publication scheduled. The video stays private until then.");
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

  return (
    <section className="workspace-card mt-8 p-5" aria-label="YouTube">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 className="font-semibold">YouTube</h2>
          <p className="mt-1 text-xs text-[var(--muted-foreground)]">
            {connection.status === "not_connected" && "Not connected"}
            {connection.status === "auth_expired" && "YouTube sign-in expired — reconnect to continue."}
            {connected && <>Connected: <strong className="text-[var(--foreground)]">{connection.channel_title}</strong></>}
          </p>
        </div>
        {!connected && (
          <Button asChild size="sm" variant="outline">
            <Link href="/settings/integrations#youtube">{connection.status === "auth_expired" ? "Reconnect" : "Connect"}</Link>
          </Button>
        )}
        {connected && !focus && current.uploadable && (
          <Button size="sm" variant="accent" disabled={locked} onClick={() => void run("upload", () => uploadProjectToYouTube(project.id, project.current_revision))}>
            {busy === "upload" ? <LoaderCircle className="size-3.5 animate-spin" /> : <Upload className="size-3.5" />} Upload privately
          </Button>
        )}
      </div>

      {connected && !current.uploadable && current.code !== "already_uploaded" && current.message && (
        <p className="mt-3 text-xs text-[var(--muted-foreground)]">{current.message}</p>
      )}
      {connected && focus && current.uploadable && focus.render_revision !== current.render_revision && (
        <div className="cf-subtle mt-3 flex flex-wrap items-center justify-between gap-2 rounded-xl border px-3 py-2 text-xs">
          <span>This revision (render v{current.render_revision}) is not on YouTube yet. Earlier revisions keep their own videos.</span>
          <Button size="sm" variant="outline" disabled={locked} onClick={() => void run("upload", () => uploadProjectToYouTube(project.id, project.current_revision))}>
            <Upload className="size-3.5" /> Upload privately
          </Button>
        </div>
      )}

      {connected && current.code === "already_uploaded" && current.message && (
        <p className="mt-3 text-xs text-[var(--muted-foreground)]">{current.message} — this exact render is not uploaded twice.</p>
      )}

      {focus && <UploadStatus
        upload={focus}
        locked={locked}
        busy={busy}
        scheduling={scheduling}
        date={date}
        time={time}
        confirmReupload={confirmReupload}
        onDate={setDate}
        onTime={setTime}
        onScheduleOpen={() => setScheduling(true)}
        onScheduleCancel={() => setScheduling(false)}
        onSchedule={() => schedule(focus)}
        onSync={() => void run("sync", () => syncYouTubeUpload(focus.id))}
        onRetry={() => void run("retry", () => retryYouTubeUpload(focus.id))}
        onRefreshAnalytics={() => void run("analytics", async () => {
          const response = await refreshYouTubeAnalytics(focus.id);
          if (response.result.status === "error" && response.result.error) throw new Error(response.result.error.message);
        })}
        onReupload={() => {
          if (!confirmReupload) { setConfirmReupload(true); return; }
          setConfirmReupload(false);
          void run("upload", () => uploadProjectToYouTube(project.id, project.current_revision, true));
        }}
      />}

      {notice && (
        <p role={notice.tone === "error" ? "alert" : "status"} className={cn("mt-3 rounded-xl px-3 py-2 text-xs", notice.tone === "error" ? "bg-red-50 text-red-800" : "bg-emerald-50 text-emerald-800")}>{notice.text}</p>
      )}

      {others.length > 0 && (
        <p className="mt-3 text-[11px] text-[var(--muted-foreground)]">
          Other revisions: {others.map((item) => `render v${item.render_revision} → ${item.youtube_video_id} (${lifecycleLabel(item)})`).join(" · ")}
        </p>
      )}

      <PerformanceSection report={data.performance} />
    </section>
  );
}

function UploadStatus({ upload, locked, busy, scheduling, date, time, confirmReupload, onDate, onTime, onScheduleOpen, onScheduleCancel, onSchedule, onSync, onRetry, onRefreshAnalytics, onReupload }: {
  upload: YouTubeUpload; locked: boolean; busy: string | null; scheduling: boolean; date: string; time: string; confirmReupload: boolean;
  onDate: (value: string) => void; onTime: (value: string) => void; onScheduleOpen: () => void; onScheduleCancel: () => void; onSchedule: () => void;
  onSync: () => void; onRetry: () => void; onRefreshAnalytics: () => void; onReupload: () => void;
}) {
  const link = upload.lifecycle === "published" ? upload.shorts_url : upload.studio_url;
  const canSchedule = upload.lifecycle === "private" && !!upload.youtube_video_id && upload.state !== "failed";
  const retryable = upload.lifecycle === "failed" && !upload.youtube_video_id && upload.error?.code !== "session_expired_unknown_outcome";
  return (
    <div className="cf-subtle mt-4 rounded-[.85rem] border p-3 text-xs">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="min-w-0">
          <p className="text-sm font-semibold">{lifecycleLabel(upload)}</p>
          <p className="mono mt-0.5 text-[10px] text-[var(--muted-foreground)]">
            {upload.youtube_video_id ? `Video ${upload.youtube_video_id}` : "No video yet"} · render v{upload.render_revision} · {upload.privacy_status}
            {upload.content_type ? ` · ${upload.content_type}` : ""}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-1">
          {link && <a href={link} target="_blank" rel="noreferrer" className="interactive-text"><ExternalLink className="size-3.5" /> Open</a>}
          {canSchedule && !scheduling && <button type="button" className="interactive-text" disabled={locked} onClick={onScheduleOpen}><CalendarClock className="size-3.5" /> Schedule</button>}
          {upload.youtube_video_id && upload.lifecycle !== "published" && <button type="button" className="interactive-text" disabled={locked} onClick={onSync}>{busy === "sync" ? <LoaderCircle className="size-3.5 animate-spin" /> : <RefreshCw className="size-3.5" />} Refresh status</button>}
          {upload.lifecycle === "published" && <button type="button" className="interactive-text" disabled={locked} onClick={onRefreshAnalytics}>{busy === "analytics" ? <LoaderCircle className="size-3.5 animate-spin" /> : <RefreshCw className="size-3.5" />} Refresh analytics</button>}
          {retryable && <button type="button" className="interactive-text" disabled={locked} onClick={onRetry}>{busy === "retry" ? <LoaderCircle className="size-3.5 animate-spin" /> : <RefreshCw className="size-3.5" />} Retry upload</button>}
        </div>
      </div>

      {upload.lifecycle === "uploading" && upload.progress !== null && (
        <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-black/10" role="progressbar" aria-valuenow={Math.round(upload.progress * 100)} aria-valuemin={0} aria-valuemax={100}>
          <div className="h-full bg-[#ff6838] transition-all" style={{ width: `${Math.round(upload.progress * 100)}%` }} />
        </div>
      )}
      {upload.lifecycle === "private" && <p className="mt-2 text-[var(--muted-foreground)]">Uploaded privately. Nothing is published until you schedule it.</p>}
      {upload.lifecycle === "scheduled" && <p className="mt-2">Scheduled for <strong>{formatDateTime(upload.publish_at)}</strong>. It stays private until then.</p>}
      {upload.lifecycle === "published" && <p className="mt-2 text-[var(--muted-foreground)]">Published {formatDateTime(upload.published_at)} · Last analytics sync: {formatDateTime(upload.last_analytics_sync_at)}</p>}
      {upload.schedule_status === "schedule_failed" && upload.schedule_error && (
        <p role="alert" className="mt-2 flex gap-1.5 text-red-800"><AlertTriangle className="mt-px size-3.5 shrink-0" /> Scheduling failed: {upload.schedule_error}</p>
      )}
      {upload.error && upload.lifecycle !== "published" && (
        <p role="alert" className="mt-2 flex gap-1.5 text-red-800"><AlertTriangle className="mt-px size-3.5 shrink-0" /> {upload.error.message}</p>
      )}
      {upload.can_reupload && upload.is_active_mapping && (
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <span className="text-[var(--muted-foreground)]">{confirmReupload ? "This creates a new YouTube video. Continue?" : "YouTube no longer has a usable copy of this render."}</span>
          <Button size="sm" variant="outline" disabled={locked} onClick={onReupload}>{confirmReupload ? "Yes, upload a new copy" : "Upload again"}</Button>
        </div>
      )}

      {scheduling && canSchedule && (
        <form className="mt-3 flex flex-wrap items-end gap-2" onSubmit={(event) => { event.preventDefault(); onSchedule(); }}>
          <label className="text-[11px] font-semibold">Date<input type="date" value={date} onChange={(event) => onDate(event.target.value)} className="cf-input mt-1 text-xs" required /></label>
          <label className="text-[11px] font-semibold">Time<input type="time" value={time} onChange={(event) => onTime(event.target.value)} className="cf-input mt-1 text-xs" required /></label>
          <Button type="submit" size="sm" variant="accent" disabled={locked || !date || !time}>{busy === "schedule" ? <LoaderCircle className="size-3.5 animate-spin" /> : <CalendarClock className="size-3.5" />} Schedule publication</Button>
          <Button type="button" size="sm" variant="ghost" onClick={onScheduleCancel}>Cancel</Button>
          <p className="w-full text-[10px] text-[var(--muted-foreground)]">Your local time. YouTube keeps the video private and publishes it at exactly this time.</p>
        </form>
      )}
    </div>
  );
}

function PerformanceSection({ report }: { report: PerformanceReport }) {
  const metrics = report.latest_snapshot?.metrics ?? {};
  const scenes = visibleSceneRows(report.scene_retention);
  const opening = report.opening_retention;
  return (
    <div className="mt-5 border-t border-black/8 pt-4" aria-label="Performance">
      <h3 className="text-sm font-semibold">Performance</h3>
      {report.status !== "ready" && <p className="mt-1 text-xs text-[var(--muted-foreground)]">{performanceHeadline(report)}</p>}
      {report.analytics_error && <p role="alert" className="mt-2 text-xs text-red-800">Analytics: {report.analytics_error.message}</p>}
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
                  <li key={scene.scene_id} className={cn("rounded-xl border px-3 py-2", scene.notable_drop ? "border-amber-300 bg-amber-50/70 text-amber-950" : "cf-subtle")}>
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
