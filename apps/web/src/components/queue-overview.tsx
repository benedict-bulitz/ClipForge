"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { flushSync } from "react-dom";
import { ArrowLeft, ArrowRight, CalendarClock, Clapperboard, ListVideo, LoaderCircle, Play, RefreshCw, Settings, Upload } from "lucide-react";
import { ApiError, getProject, getQueueOverview, listGenerationJobs, mediaUrl } from "@/lib/api";
import { formatDuration, generationTimeLabel, POLL_TIMEOUT_MS, withTimeout } from "@/lib/generation-poll";
import { createHomePoller, type HomePoller } from "@/lib/home-poll";
import {
  createExclusivePlayback,
  mergeLiveJobs,
  overviewRefreshNeeded,
  playableSource,
  queueProgressPercent,
  queuePublicationLines,
  queueQualityBadge,
  queueRowStatus,
  queueUploadAction,
  uploadInFlight,
  type ExclusivePlayback,
  type QueueItem,
  type QueueOverview,
  type QueueRowTone,
} from "@/lib/queue-page";
import { PLATFORM_LABELS, type ProjectPublication } from "@/lib/publishing";
import type { GenerationJob, Project } from "@/lib/types";
import { browserLocale, lifecycleLabel, scheduleLine, type YouTubeUpload } from "@/lib/youtube";
import { cn } from "@/lib/utils";
import { toneClass } from "@/lib/alerts";
import { Brand } from "./brand";
import { ThemeToggle } from "./theme-toggle";
import { Alert } from "./ui/alert";
import { Button } from "./ui/button";
import { UploadSheet } from "./upload-sheet";

/** While an upload continues in the background, its row is re-read on this cadence (bounded). */
const UPLOAD_FOLLOW_UP_MS = 5000;
const MAX_UPLOAD_FOLLOW_UPS = 120;

function errorText(reason: unknown, fallback: string): string {
  return reason instanceof Error ? reason.message : fallback;
}

export function QueueOverviewPage() {
  const [overview, setOverview] = useState<QueueOverview | null>(null);
  const [jobs, setJobs] = useState<GenerationJob[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [publishing, setPublishing] = useState<Project | null>(null);
  const [opening, setOpening] = useState<string | null>(null);
  const [rowErrors, setRowErrors] = useState<Record<string, string>>({});
  const poller = useRef<HomePoller | null>(null);
  const [playback] = useState<ExclusivePlayback>(() => createExclusivePlayback());
  const followUps = useRef(0);

  useEffect(() => {
    let mounted = true;
    let signature: string | null = null;
    // The page's one polling authority (home-poll.ts): jobs on the active/idle
    // cadence, the overview only on start, on status changes, and on focus.
    const instance: HomePoller = createHomePoller<QueueOverview>({
      loadJobs: () => withTimeout((signal) => listGenerationJobs(signal), POLL_TIMEOUT_MS),
      loadProjects: () => withTimeout((signal) => getQueueOverview(signal), POLL_TIMEOUT_MS).catch((reason: unknown) => {
        if (mounted) setLoadError(errorText(reason, "The queue could not be loaded."));
        throw reason;
      }),
      onJobs: (next) => {
        if (!mounted) return;
        setJobs(next);
        const change = overviewRefreshNeeded(signature, next);
        signature = change.signature;
        if (change.refresh) void instance.refreshProjects();
      },
      onProjects: (next) => {
        if (!mounted) return;
        setOverview(next);
        setLoadError(null);
      },
    });
    poller.current = instance;
    instance.start();
    return () => {
      mounted = false;
      instance.stop();
      if (poller.current === instance) poller.current = null;
    };
  }, []);

  const uploading = uploadInFlight(overview);
  useEffect(() => {
    if (!uploading || followUps.current >= MAX_UPLOAD_FOLLOW_UPS) return;
    const timer = window.setTimeout(() => {
      followUps.current += 1;
      void poller.current?.refreshProjects();
    }, UPLOAD_FOLLOW_UP_MS);
    return () => window.clearTimeout(timer);
  }, [uploading, overview]);

  /** "Upload": load the current project, then open the one unified Upload sheet. */
  async function openPublishing(projectId: string) {
    if (opening) return;
    setOpening(projectId);
    setRowErrors((errors) => Object.fromEntries(Object.entries(errors).filter(([id]) => id !== projectId)));
    playback.pauseAll();
    try {
      setPublishing(await getProject(projectId));
    } catch (reason) {
      const message = reason instanceof ApiError && reason.statusCode === 404 ? "This project no longer exists." : errorText(reason, "The project could not be loaded.");
      setRowErrors((errors) => ({ ...errors, [projectId]: message }));
      void poller.current?.refreshProjects();
    } finally {
      setOpening(null);
    }
  }

  function closePublishing() {
    setPublishing(null);
    followUps.current = 0;
    void poller.current?.refreshProjects();
  }

  const items = overview ? mergeLiveJobs(overview.items, jobs) : [];
  const counts = {
    waiting: items.filter((item) => item.job.status === "queued").length,
    running: items.filter((item) => item.job.status === "running" || item.job.status === "cancelling").length,
    finished: items.filter((item) => !["queued", "running", "cancelling"].includes(item.job.status)).length,
  };

  return (
    <main className="theme-app min-h-screen bg-[var(--background)]">
      <header className="sticky top-0 z-40 border-b border-[var(--border)] bg-[var(--surface)] backdrop-blur-xl">
        <div className="mx-auto flex min-h-16 max-w-[1100px] items-center justify-between gap-4 px-4 sm:px-6">
          <Link href="/" aria-label="ClipForge home"><Brand /></Link>
          <nav aria-label="Main" className="flex items-center gap-1">
            <ThemeToggle />
            <Button asChild variant="ghost" size="sm"><Link href="/videos"><Clapperboard className="size-3.5" /> <span className="hidden sm:inline">Videos</span></Link></Button>
            <Button asChild variant="ghost" size="sm"><Link href="/settings/integrations"><Settings className="size-3.5" /> <span className="hidden sm:inline">Settings</span></Link></Button>
            <Button asChild variant="ghost" size="sm"><Link href="/"><ArrowLeft className="size-3.5" /> <span className="hidden sm:inline">Studio</span></Link></Button>
          </nav>
        </div>
      </header>

      <section className="mx-auto max-w-[1100px] px-4 pb-20 pt-8 sm:px-6" aria-labelledby="queue-overview-title">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <h1 id="queue-overview-title" className="flex items-center gap-2 text-2xl font-semibold tracking-[-.03em]"><ListVideo className="size-5 text-[var(--accent)]" /> Queue Overview</h1>
            <p className="mt-1 text-sm text-[var(--muted-foreground)]">
              The current queue: everything waiting or generating, plus what finished since the queue was last idle.
            </p>
          </div>
          {overview && items.length > 0 && (
            <p className="mono text-[10px] uppercase tracking-[.1em] text-[var(--muted-foreground)]" aria-live="polite">
              {counts.running} generating · {counts.waiting} queued · {counts.finished} finished
            </p>
          )}
        </div>

        {loadError && (
          <Alert tone="error" size="lg" className="mt-5 items-center" action={<button type="button" className="inline-flex items-center gap-1 font-semibold underline" onClick={() => void poller.current?.refreshAll()}><RefreshCw className="size-3.5" /> Retry</button>}>
            {loadError}
          </Alert>
        )}

        {!overview && !loadError && (
          <p className="mt-10 flex items-center gap-2 text-sm text-[var(--muted-foreground)]" role="status"><LoaderCircle className="size-4 animate-spin" /> Loading the queue…</p>
        )}

        {overview && items.length === 0 && (
          <div className="workspace-card mt-8 p-6 text-center">
            <p className="font-semibold">The queue is empty.</p>
            <p className="mt-1 text-sm text-[var(--muted-foreground)]">Videos you generate in the Studio appear here while they are made.</p>
            <Button asChild variant="accent" size="sm" className="mt-4"><Link href="/">Open the Studio <ArrowRight className="size-3.5" /></Link></Button>
          </div>
        )}

        {overview && items.length > 0 && (
          <ol className="mt-6 grid gap-3" aria-label="Queued and generated videos">
            {items.map((item) => (
              <QueueRow
                key={item.job.id}
                item={item}
                overview={overview}
                playback={playback}
                opening={opening === item.job.project_id}
                locked={opening !== null || publishing !== null}
                error={rowErrors[item.job.project_id] ?? null}
                onUpload={() => void openPublishing(item.job.project_id)}
                onPlayerError={() => void poller.current?.refreshProjects()}
              />
            ))}
          </ol>
        )}
      </section>

      {publishing && <UploadSheet project={publishing} onClose={closePublishing} onUploaded={closePublishing} />}
    </main>
  );
}

/** Row status pills: semantic tones; "active" (generating) keeps the brand accent. */
const TONE_CLASS: Record<QueueRowTone, string> = {
  waiting: toneClass("muted"),
  active: "bg-[#ff6838]/10 text-[#d94c20]",
  ok: toneClass("success"),
  muted: toneClass("muted"),
  error: toneClass("error"),
};

function QueueRow({ item, overview, playback, opening, locked, error, onUpload, onPlayerError }: {
  item: QueueItem;
  overview: QueueOverview;
  playback: ExclusivePlayback;
  opening: boolean;
  locked: boolean;
  error: string | null;
  onUpload: () => void;
  onPlayerError: () => void;
}) {
  const { job, project } = item;
  const status = queueRowStatus(item);
  const progress = queueProgressPercent(job);
  const timing = job.status === "running" ? generationTimeLabel(job) : null;
  const badge = queueQualityBadge(project);
  const action = queueUploadAction(overview, item);
  const source = playableSource(item);
  const upload = project?.youtube.upload ?? null;
  const title = project?.title || job.prompt;
  return (
    <li className="queue-row workspace-card grid grid-cols-[auto_minmax(0,1fr)] gap-4 p-3 sm:p-4 lg:grid-cols-[auto_minmax(0,1fr)_220px_176px] lg:items-center">
      <InlinePreview
        key={source ?? `${job.id}:${status.state}`}
        id={job.id}
        title={title}
        source={source}
        poster={project?.poster_url ?? null}
        duration={project?.duration_seconds ?? null}
        status={status.state}
        playback={playback}
        onError={onPlayerError}
      />

      <div className="min-w-0 self-center">
        <p className="break-words text-sm font-semibold leading-5">{job.prompt}</p>
        {project && project.title !== job.prompt && <p className="mt-0.5 truncate text-xs text-[var(--muted-foreground)]">{project.title}</p>}
        <p className="mono mt-1.5 text-[9px] uppercase tracking-[.1em] text-[var(--muted-foreground)]">
          {project ? `v${project.current_revision}` : "No project yet"}{project?.duration_seconds ? ` · ${formatDuration(project.duration_seconds)}` : ""}
        </p>
        {/* On narrow screens the status column folds in here. */}
        <div className="mt-2 lg:hidden"><StatusBlock status={status} progress={progress} timing={timing} badge={badge} upload={upload} publications={queuePublicationLines(project)} /></div>
      </div>

      <div className="hidden lg:block"><StatusBlock status={status} progress={progress} timing={timing} badge={badge} upload={upload} publications={queuePublicationLines(project)} /></div>

      {/* text-xs here: a global `button { font: inherit }` rule overrides the Button's own size. */}
      <div className="col-span-2 flex flex-wrap items-center justify-end gap-2 text-xs lg:col-span-1 lg:flex-col lg:items-stretch">
        {action.kind === "upload" && (
          <Button size="sm" variant="accent" disabled={locked} onClick={onUpload}>
            {opening ? <LoaderCircle className="size-3.5 animate-spin" /> : <Upload className="size-3.5" />} {action.label}
          </Button>
        )}
        {action.kind === "connect" && (
          <Button asChild size="sm" variant="outline"><Link href="/settings/integrations">{action.label}</Link></Button>
        )}
        {status.state !== "unavailable" && (
          <Button asChild size="sm" variant="outline"><Link href={`/projects/${job.project_id}`}>View Details <ArrowRight className="size-3.5" /></Link></Button>
        )}
        {action.kind === "none" && action.reason && <p className="text-[11px] text-[var(--muted-foreground)]">{action.reason}</p>}
        {error && <p role="alert" className="cf-text-error text-[11px] font-medium">{error}</p>}
      </div>
    </li>
  );
}

function StatusBlock({ status, progress, timing, badge, upload, publications }: {
  status: ReturnType<typeof queueRowStatus>;
  progress: number | null;
  timing: string | null;
  badge: ReturnType<typeof queueQualityBadge>;
  upload: YouTubeUpload | null;
  publications: ProjectPublication[];
}) {
  const locale = browserLocale();
  const current = upload?.current ?? null;
  const zone = current?.requested.timezone ?? "UTC";
  return (
    <div className="min-w-0 space-y-1.5 text-xs" aria-live="polite">
      <div className="flex flex-wrap items-center gap-1.5">
        <span className={cn("inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-bold uppercase tracking-[.06em]", TONE_CLASS[status.tone])}>
          {status.state === "running" && <LoaderCircle className="size-3 animate-spin" aria-hidden />}{status.label}{progress !== null ? ` · ${progress}%` : ""}
        </span>
        {badge && (
          <span title={badge.title ?? undefined} className={cn("inline-flex rounded-full px-2 py-0.5 text-[10px] font-bold uppercase tracking-[.06em]", badge.tone === "ok" ? TONE_CLASS.ok : badge.tone === "attention" ? toneClass("warning") : TONE_CLASS.muted)}>
            {badge.label}
          </span>
        )}
      </div>
      {progress !== null && (
        <div className="h-1.5 overflow-hidden rounded-full bg-[#ff6838]/15" role="progressbar" aria-label="Generation progress" aria-valuemin={0} aria-valuemax={100} aria-valuenow={progress}>
          <div className="h-full bg-[#ff6838] transition-[width] duration-500" style={{ width: `${progress}%` }} />
        </div>
      )}
      {status.detail && <p className={cn("break-words leading-4", status.tone === "error" ? "cf-text-error" : "text-[var(--muted-foreground)]")}>{status.detail}</p>}
      {timing && <p className="mono text-[10px] text-[var(--muted-foreground)]">{timing}</p>}
      {upload && current && (
        <p className="flex items-center gap-1 text-[var(--muted-foreground)]">
          <CalendarClock className="size-3 shrink-0" aria-hidden />
          <span>YouTube: <strong className="font-semibold text-[var(--foreground)]">{lifecycleLabel(upload)}</strong>{current.state === "scheduled" && current.scheduled_for ? ` · ${scheduleLine(current.scheduled_for, zone, locale)}` : ""}</span>
        </p>
      )}
      {publications.map((item) => (
        <p key={item.id} className="flex items-center gap-1 text-[var(--muted-foreground)]">
          <CalendarClock className="size-3 shrink-0" aria-hidden />
          <span className="truncate">{PLATFORM_LABELS[item.platform]} · {item.account_label}: <strong className="font-semibold text-[var(--foreground)]">{item.state_label}</strong>{item.state === "scheduled" && item.scheduled_at ? ` · ${new Date(item.scheduled_at).toLocaleString(locale)}` : ""}</span>
        </p>
      ))}
    </div>
  );
}

/**
 * The left preview area IS the player.  Before a click it is only a poster
 * (no <video>, nothing downloaded); the click mounts the final MP4 in the same
 * small 9:16 box and starts it with sound - a user gesture, never autoplay.
 */
function InlinePreview({ id, title, source, poster, duration, status, playback, onError }: {
  id: string;
  title: string;
  source: string | null;
  poster: string | null;
  duration: number | null;
  status: ReturnType<typeof queueRowStatus>["state"];
  playback: ExclusivePlayback;
  onError: () => void;
}) {
  const [active, setActive] = useState(false);
  const [failed, setFailed] = useState(false);
  const [posterBroken, setPosterBroken] = useState(false);
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const posterSource = poster && !posterBroken ? mediaUrl(poster) : null;
  const videoSource = mediaUrl(source);

  useEffect(() => {
    const element = videoRef.current;
    if (!active || !element) return;
    const unregister = playback.register(id, element);
    return () => {
      element.pause();
      unregister();
    };
  }, [active, id, playback]);

  function start() {
    if (!videoSource) return;
    setFailed(false);
    // Mount the player inside this click so play() keeps the user gesture (sound allowed).
    flushSync(() => setActive(true));
    const element = videoRef.current;
    if (!element) return;
    playback.playing(id);
    void element.play().catch(() => { /* blocked or still loading: the controls stay usable */ });
  }

  const frame = "relative aspect-[9/16] w-[76px] shrink-0 overflow-hidden rounded-xl border border-black/10 bg-[#12120f] sm:w-[88px] dark:border-white/10";

  if (active && videoSource && !failed) {
    return (
      <div className={frame}>
        <video
          ref={videoRef}
          src={videoSource}
          poster={posterSource ?? undefined}
          controls
          playsInline
          preload="metadata"
          className="size-full bg-black object-contain"
          aria-label={`Final video: ${title}`}
          onPlay={() => playback.playing(id)}
          onError={() => { setFailed(true); setActive(false); onError(); }}
        />
      </div>
    );
  }

  const art = posterSource ? (
    // eslint-disable-next-line @next/next/no-img-element -- local media, already sized; next/image adds nothing here
    <img src={posterSource} alt="" loading="lazy" className="size-full object-cover" onError={() => setPosterBroken(true)} />
  ) : (
    <div className="absolute inset-0 bg-[radial-gradient(circle_at_66%_22%,#ffb178_0,transparent_28%),linear-gradient(155deg,#7d2a16_0%,#191914_50%,#080809_100%)]">
      <p className="absolute inset-x-1.5 bottom-6 line-clamp-4 text-center text-[8px] font-extrabold uppercase leading-[1.25] tracking-[-.02em] text-white/85">{title}</p>
    </div>
  );

  if (!videoSource) {
    const note = failed ? null : status === "running" ? "Generating" : status === "queued" ? "Waiting" : status === "cancelling" ? "Stopping" : null;
    return (
      <div className={frame} aria-label={`Preview of ${title}`}>
        {art}
        <span className="absolute inset-0 bg-black/35" />
        {note && <span className="absolute inset-x-0 top-1/2 -translate-y-1/2 text-center text-[9px] font-bold uppercase tracking-[.1em] text-white/90">{status === "running" && <LoaderCircle className="mx-auto mb-1 size-4 animate-spin" aria-hidden />}{note}</span>}
      </div>
    );
  }

  return (
    <button type="button" onClick={start} className={cn(frame, "group text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#ff6838]/50")} aria-label={failed ? `Retry playing ${title}` : `Play ${title}`}>
      {art}
      <span className="absolute inset-0 bg-gradient-to-b from-transparent via-transparent to-black/55 transition-colors group-hover:bg-black/20" />
      <span className="absolute left-1/2 top-1/2 grid size-9 -translate-x-1/2 -translate-y-1/2 place-items-center rounded-full bg-black/45 text-white shadow-[0_4px_14px_rgba(0,0,0,.35)] backdrop-blur-sm transition-transform duration-150 group-hover:scale-105">
        {failed ? <RefreshCw className="size-4" /> : <Play className="ml-0.5 size-4 fill-current" />}
      </span>
      {duration ? <span className="mono absolute bottom-1.5 right-1.5 rounded bg-black/65 px-1 py-px text-[9px] font-semibold text-white">{formatDuration(duration)}</span> : null}
      {failed && <span role="alert" className="absolute inset-x-1 top-1.5 rounded bg-black/70 px-1 py-0.5 text-center text-[8px] font-semibold leading-3 text-white">Video could not be loaded</span>}
    </button>
  );
}
