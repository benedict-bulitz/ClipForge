"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowLeft, CalendarClock, Clapperboard, ExternalLink, Film, LoaderCircle, RefreshCw, Search, Settings } from "lucide-react";
import { ApiError, listVideos, mediaUrl, refreshRecentVideos } from "@/lib/api";
import {
  ANALYTICS_OPTIONS,
  PAGE_SIZE,
  PLATFORM_OPTIONS,
  PROJECT_OPTIONS,
  SORT_OPTIONS,
  STATUS_OPTIONS,
  accountFilterOptions,
  analyticsStateLabel,
  analyticsValue,
  isSocialVideo,
  dateLine,
  formatClock,
  formatCount,
  formatPercent,
  formatViewDuration,
  libraryQuery,
  liveStatValue,
  liveStatsNote,
  projectHref,
  projectLabel,
  refreshNotice,
  sameFilters,
  stateTone,
  summaryLine,
  type AnyLibraryVideo,
  type LibraryFilters,
  type LibraryVideo,
  type SocialLibraryVideo,
  type VideoLibraryPage,
} from "@/lib/videos";
import { PLATFORM_LABELS, PRIVACY_LABELS } from "@/lib/publishing";
import { browserLocale, detectTimeZone, scheduleLine } from "@/lib/youtube";
import { cn } from "@/lib/utils";
import { Brand } from "./brand";
import { ChannelPerformance } from "./channel-performance";
import { Button } from "./ui/button";
import { ThemeToggle } from "./theme-toggle";

const SEARCH_DEBOUNCE_MS = 250;

function shortDate(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}

export function StateChip({ video }: { video: Pick<LibraryVideo, "state" | "state_label" | "processing"> | Pick<SocialLibraryVideo, "state" | "state_label"> }) {
  const tone = stateTone(video.state);
  return (
    <span className={cn(
      "inline-flex items-center rounded-full px-2 py-0.5 text-[10px] font-bold uppercase tracking-[.06em]",
      tone === "ok" && "bg-emerald-50 text-emerald-800 dark:bg-emerald-500/15 dark:text-emerald-300",
      tone === "info" && "bg-sky-50 text-sky-800 dark:bg-sky-500/15 dark:text-sky-300",
      tone === "muted" && "bg-black/5 text-[var(--muted-foreground)] dark:bg-white/10",
      tone === "warn" && "bg-amber-50 text-amber-800 dark:bg-amber-500/15 dark:text-amber-300",
      tone === "error" && "bg-red-50 text-red-800 dark:bg-red-500/15 dark:text-red-300",
    )}>
      {video.state_label}{"processing" in video && video.processing && video.state !== "processing" ? " · Processing" : ""}
    </span>
  );
}

/** The retained small preview, or a neutral placeholder (archived records may have none). */
export function VideoThumbnail({ video, className }: { video: Pick<LibraryVideo, "thumbnail_url" | "title">; className?: string }) {
  const [failed, setFailed] = useState(false);
  const src = video.thumbnail_url && !failed ? mediaUrl(video.thumbnail_url) : null;
  return (
    <div className={cn("relative aspect-[9/16] shrink-0 overflow-hidden rounded-lg border border-[var(--border)] bg-black/5 dark:bg-white/5", className)}>
      {src ? (
        // eslint-disable-next-line @next/next/no-img-element
        <img src={src} alt="" loading="lazy" onError={() => setFailed(true)} className="size-full object-cover" />
      ) : (
        <div className="grid size-full place-items-center text-[var(--muted-foreground)]" aria-label="No preview">
          <Film className="size-4" />
        </div>
      )}
    </div>
  );
}

/** The exact schedule in the zone it was chosen in (existing schedule formatting). */
export function scheduledTime(iso: string, timezone: string | null): string {
  const zone = timezone ?? detectTimeZone();
  return `${scheduleLine(iso, zone, browserLocale())} · ${zone}`;
}

/** YouTube/Studio actions: links while the remote video exists, disabled with a reason otherwise. */
function YouTubeLinks({ video }: { video: LibraryVideo }) {
  if (!video.youtube_actions.available) {
    return (
      <>
        <span className="text-[10px] text-[var(--muted-foreground)]">Removed from YouTube</span>
        {["YouTube", "Studio"].map((label) => (
          <span key={label} role="link" aria-disabled="true" title={video.youtube_actions.reason ?? undefined} className="interactive-text cursor-not-allowed opacity-40"><ExternalLink className="size-3" /> {label}</span>
        ))}
      </>
    );
  }
  return (
    <>
      {video.watch_url && <a href={video.shorts_url ?? video.watch_url} target="_blank" rel="noreferrer" className="interactive-text"><ExternalLink className="size-3" /> YouTube</a>}
      {video.studio_url && <a href={video.studio_url} target="_blank" rel="noreferrer" className="interactive-text"><ExternalLink className="size-3" /> Studio</a>}
    </>
  );
}

function VideoRow({ video }: { video: LibraryVideo }) {
  const href = `/videos/${video.id}`;
  const project = projectHref(video);
  const state = video.analytics.state;
  const scheduled = video.state === "scheduled" && video.scheduled_for;
  const liveNote = liveStatsNote(video.live_stats_state);
  const statsNote = video.live_stats_state === "not_published" && state === "not_published" ? "Stats start after publication" : `Analytics: ${analyticsStateLabel(state)}`;
  return (
    <li className="workspace-card flex gap-3 p-3 sm:gap-4" data-video-id={video.youtube_video_id}>
      <Link href={href} aria-hidden tabIndex={-1}><VideoThumbnail video={video} className="w-14 sm:w-16" /></Link>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-start justify-between gap-x-3 gap-y-1">
          <div className="min-w-0">
            <Link href={href} className="line-clamp-2 break-words font-semibold hover:underline">{video.title}</Link>
            <p className={cn("mt-0.5 flex items-center gap-1 text-xs", scheduled ? "font-semibold text-sky-800 dark:text-sky-300" : "font-medium")} aria-label="Date">
              {scheduled && <CalendarClock className="size-3.5 shrink-0" />}
              {dateLine(video, shortDate, (iso) => scheduledTime(iso, video.schedule_timezone))}
            </p>
            <p className="mt-0.5 truncate text-[11px] text-[var(--muted-foreground)]">
              {video.channel.title ?? video.channel.id}
              {video.duration_seconds !== null ? ` · ${formatClock(video.duration_seconds)}` : ""}
              {(video.topic || video.prompt) && <span title={video.prompt ?? undefined}> · Prompt: {video.topic ?? video.prompt}</span>}
            </p>
          </div>
          <StateChip video={video} />
        </div>
        <dl className="mt-2 grid grid-cols-3 gap-x-3 gap-y-1 text-[11px] sm:grid-cols-6" aria-label="Performance summary">
          <div title={liveNote ?? "YouTube Data API (live)"}><dt className="text-[var(--muted-foreground)]">Views</dt><dd className="font-semibold">{liveStatValue(video, "views")}</dd></div>
          <div title={liveNote ?? "YouTube Data API (live)"}><dt className="text-[var(--muted-foreground)]">Likes</dt><dd className="font-semibold">{liveStatValue(video, "likes")}</dd></div>
          <div title={liveNote ?? "YouTube Data API (live)"}><dt className="text-[var(--muted-foreground)]">Comments</dt><dd className="font-semibold">{liveStatValue(video, "comments")}</dd></div>
          <div title="YouTube Analytics API"><dt className="text-[var(--muted-foreground)]">Engaged</dt><dd className="font-semibold">{analyticsValue(video.analytics.engagedViews, state, (value) => formatCount(value))}</dd></div>
          <div title="YouTube Analytics API"><dt className="text-[var(--muted-foreground)]">Avg view</dt><dd className="font-semibold">{analyticsValue(video.analytics.averageViewDuration, state, formatViewDuration)}</dd></div>
          <div title="YouTube Analytics API"><dt className="text-[var(--muted-foreground)]">Avg view %</dt><dd className="font-semibold">{analyticsValue(video.analytics.averageViewPercentage, state, formatPercent)}</dd></div>
        </dl>
        <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px]">
          <span className={cn("font-semibold", video.project.available ? "text-emerald-700 dark:text-emerald-300" : "text-[var(--muted-foreground)]")}>{projectLabel(video)}</span>
          <span className="text-[var(--muted-foreground)]">{statsNote}</span>
          {video.stale && <span className="text-amber-700 dark:text-amber-300">Status may be outdated</span>}
          <span className="mono text-[10px] text-[var(--muted-foreground)]" title="YouTube video ID">ID {video.youtube_video_id}</span>
          <span className="ml-auto flex items-center gap-1">
            {project && <Link href={project} className="interactive-text">Open project</Link>}
            <YouTubeLinks video={video} />
          </span>
        </div>
      </div>
    </li>
  );
}

/** An Instagram/TikTok publication: its own state, account and link (no YouTube analytics). */
function SocialVideoRow({ video }: { video: SocialLibraryVideo }) {
  const project = projectHref(video);
  const scheduled = (video.state === "scheduled" || video.state === "missed") && video.scheduled_for;
  return (
    <li className="workspace-card flex gap-3 p-3 sm:gap-4" data-publication-id={video.id} data-platform={video.platform}>
      <VideoThumbnail video={video} className="w-14 sm:w-16" />
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-start justify-between gap-x-3 gap-y-1">
          <div className="min-w-0">
            <p className="line-clamp-2 break-words font-semibold">{video.title}</p>
            <p className={cn("mt-0.5 flex items-center gap-1 text-xs", scheduled ? "font-semibold text-sky-800 dark:text-sky-300" : "font-medium")}>
              {scheduled && <CalendarClock className="size-3.5 shrink-0" />}
              {scheduled && video.scheduled_for ? `Scheduled for ${scheduledTime(video.scheduled_for, video.schedule_timezone)}` : video.published_at ? `Published ${shortDate(video.published_at)}` : `Created ${shortDate(video.uploaded_at ?? video.sort_date)}`}
            </p>
            <p className="mt-0.5 truncate text-[11px] text-[var(--muted-foreground)]">
              {PLATFORM_LABELS[video.platform]} · {video.account.handle ? `@${video.account.handle}` : video.account.label}
              {video.privacy_level ? ` · ${PRIVACY_LABELS[video.privacy_level] ?? video.privacy_level}` : ""}
            </p>
          </div>
          <StateChip video={video} />
        </div>
        {video.error?.message && video.state !== "published" && <p className="mt-2 text-[11px] text-[var(--muted-foreground)]">{video.error.message}</p>}
        <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px]">
          <span className={cn("font-semibold", video.project.available ? "text-emerald-700 dark:text-emerald-300" : "text-[var(--muted-foreground)]")}>{projectLabel(video)}</span>
          <span className="text-[var(--muted-foreground)]">Analytics: not collected for {PLATFORM_LABELS[video.platform]}</span>
          <span className="ml-auto flex items-center gap-1">
            {project && <Link href={project} className="interactive-text">Open project</Link>}
            {video.remote_url && <a href={video.remote_url} target="_blank" rel="noreferrer" className="interactive-text"><ExternalLink className="size-3" /> {PLATFORM_LABELS[video.platform]}</a>}
          </span>
        </div>
      </div>
    </li>
  );
}

function Select<T extends string>({ label, value, options, onChange }: { label: string; value: T; options: Array<[T, string]>; onChange: (value: T) => void }) {
  return (
    <label className="text-[11px] font-semibold">
      <span className="sr-only">{label}</span>
      <select aria-label={label} value={value} onChange={(event) => onChange(event.target.value as T)} className="cf-input h-9 py-0 text-xs">
        {options.map(([key, text]) => <option key={key} value={key}>{text}</option>)}
      </select>
    </label>
  );
}

function errorText(reason: unknown, fallback: string): string {
  return reason instanceof ApiError || reason instanceof Error ? reason.message : fallback;
}

/** Every video uploaded through ClipForge; reads persisted state only (no YouTube call on open). */
export function VideoLibrary({ initialFilters }: { initialFilters: LibraryFilters }) {
  const router = useRouter();
  const [filters, setFilters] = useState<LibraryFilters>(initialFilters);
  const [search, setSearch] = useState(initialFilters.q);
  const [page, setPage] = useState<VideoLibraryPage | null>(null);
  const [items, setItems] = useState<AnyLibraryVideo[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [performanceKey, setPerformanceKey] = useState(0);
  const sequence = useRef(0);

  /** The first page for these filters; a newer request always wins. */
  const firstPage = useCallback((next: LibraryFilters, signal?: AbortSignal) => {
    const current = ++sequence.current;
    return listVideos(libraryQuery(next, { limit: PAGE_SIZE }), signal)
      .then((result) => {
        if (current !== sequence.current) return;
        setPage(result);
        setItems(result.items);
        setError(null);
      })
      .catch((reason) => {
        if (signal?.aborted || current !== sequence.current) return;
        setError(errorText(reason, "The video library could not be loaded."));
      })
      .finally(() => {
        if (current === sequence.current) setLoading(false);
      });
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void firstPage(filters, controller.signal);
    router.replace(`/videos${libraryQuery(filters)}`, { scroll: false });
    return () => controller.abort();
  }, [filters, firstPage, router]);

  function update(patch: Partial<LibraryFilters>) {
    const next = { ...filters, ...patch };
    if (sameFilters(filters, next)) return;
    setLoading(true);
    setFilters(next);
  }

  // Local search only: debounced, never a YouTube call per keystroke.
  useEffect(() => {
    const timer = window.setTimeout(() => {
      if (search !== filters.q) {
        setLoading(true);
        setFilters((current) => ({ ...current, q: search }));
      }
    }, SEARCH_DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [search, filters.q]);

  async function loadMore() {
    if (!page || page.next_offset === null || loadingMore) return;
    setLoadingMore(true);
    const current = sequence.current;
    try {
      const result = await listVideos(libraryQuery(filters, { limit: PAGE_SIZE, offset: page.next_offset }));
      if (current !== sequence.current) return;
      setPage(result);
      setItems((existing) => [...existing, ...result.items.filter((item) => !existing.some((known) => known.id === item.id))]);
    } catch (reason) {
      setError(errorText(reason, "More videos could not be loaded."));
    } finally {
      setLoadingMore(false);
    }
  }

  async function refreshRecent() {
    setRefreshing(true);
    setNotice(null);
    try {
      const result = await refreshRecentVideos();
      setNotice(refreshNotice(result));
      setLoading(true);
      setPerformanceKey((key) => key + 1);
      await firstPage(filters);
    } catch (reason) {
      setNotice(errorText(reason, "Recent videos could not be refreshed."));
    } finally {
      setRefreshing(false);
    }
  }

  const summary = page?.summary;
  const filtered = !sameFilters(filters, { ...filters, status: "all", project: "all", analytics: "all", q: "", platform: "all", account: "" });
  const youtubeVisible = filters.platform === "all" || filters.platform === "youtube";

  return (
    <main className="theme-app min-h-screen bg-[var(--background)]">
      <header className="sticky top-0 z-40 border-b border-[var(--border)] bg-[var(--surface)] backdrop-blur-xl">
        <div className="mx-auto flex min-h-16 max-w-[1100px] items-center justify-between gap-4 px-4 sm:px-6">
          <Link href="/" aria-label="ClipForge home"><Brand /></Link>
          <nav aria-label="Main" className="flex items-center gap-1">
            <ThemeToggle />
            <Button asChild variant="ghost" size="sm"><Link href="/videos" aria-current="page"><Clapperboard className="size-3.5" /> <span className="hidden sm:inline">Videos</span></Link></Button>
            <Button asChild variant="ghost" size="sm"><Link href="/settings/integrations"><Settings className="size-3.5" /> <span className="hidden sm:inline">Settings</span></Link></Button>
            <Button asChild variant="ghost" size="sm"><Link href="/"><ArrowLeft className="size-3.5" /> <span className="hidden sm:inline">Studio</span></Link></Button>
          </nav>
        </div>
      </header>
      <div className="mx-auto max-w-[1100px] px-4 pb-20 pt-8 sm:px-6">
        <div className="flex flex-wrap items-end justify-between gap-3 text-xs">
          <div>
            <h1 className="text-3xl font-semibold tracking-[-.045em]">Videos</h1>
            {summary ? (
              <p className="mt-1 text-sm text-[var(--muted-foreground)]" aria-label="Library summary">
                {summaryLine(summary).join(" · ")}
                {summary.median_average_view_percentage !== null && ` · median avg view ${formatPercent(summary.median_average_view_percentage)} (latest per video, n=${summary.median_average_view_percentage_n})`}
              </p>
            ) : <p className="mt-1 text-sm text-[var(--muted-foreground)]">Everything published through ClipForge on YouTube, Instagram and TikTok.</p>}
          </div>
          <Button variant="outline" size="sm" onClick={() => void refreshRecent()} disabled={refreshing || page?.connection.status !== "connected"} title={page?.connection.status !== "connected" ? "Connect YouTube in Settings to refresh" : "Ask YouTube about the newest videos only"}>
            {refreshing ? <LoaderCircle className="size-3.5 animate-spin" /> : <RefreshCw className="size-3.5" />} Refresh recent videos
          </Button>
        </div>
        {notice && <p role="status" className="mt-3 text-xs text-[var(--muted-foreground)]">{notice}</p>}

        {youtubeVisible && <ChannelPerformance refreshKey={performanceKey} />}

        <div className="mt-5 flex flex-wrap items-center gap-2" role="search">
          <label className="relative min-w-[220px] flex-1">
            <span className="sr-only">Search videos</span>
            <Search className="pointer-events-none absolute left-3 top-1/2 size-3.5 -translate-y-1/2 text-[var(--muted-foreground)]" />
            <input type="search" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Search title, prompt or video ID" className="cf-input h-9 pl-8 text-xs" maxLength={200} />
          </label>
          <Select label="Platform" value={filters.platform} options={PLATFORM_OPTIONS} onChange={(platform) => update({ platform, account: "" })} />
          <Select label="Account" value={filters.account} options={accountFilterOptions(page?.accounts, filters.platform)} onChange={(account) => update({ account })} />
          <Select label="Status" value={filters.status} options={STATUS_OPTIONS} onChange={(status) => update({ status })} />
          <Select label="Project" value={filters.project} options={PROJECT_OPTIONS} onChange={(project) => update({ project })} />
          <Select label="Analytics" value={filters.analytics} options={ANALYTICS_OPTIONS} onChange={(analytics) => update({ analytics })} />
          <Select label="Sort" value={filters.sort} options={SORT_OPTIONS} onChange={(sort) => update({ sort })} />
        </div>

        {error && <p role="alert" className="mt-5 rounded-xl bg-red-50 px-3 py-2 text-sm text-red-800">{error}</p>}
        {loading && !page && <p className="mt-6 flex items-center gap-2 text-sm text-[var(--muted-foreground)]"><LoaderCircle className="size-4 animate-spin" /> Loading…</p>}
        {page && !loading && items.length === 0 && (
          <p className="mt-6 text-sm text-[var(--muted-foreground)]">
            {filtered ? "No videos match these filters." : "No published videos yet. Upload a rendered project and it appears here."}
          </p>
        )}
        {page && (
          <p className="mt-4 text-[11px] text-[var(--muted-foreground)]" aria-live="polite">
            {loading ? "Updating…" : `${page.total} video${page.total === 1 ? "" : "s"}${filtered ? " match" : ""}`}
            {filters.sort !== "newest" && filters.sort !== "oldest" ? " · videos without this metric are listed last" : ""}
          </p>
        )}
        <ul className={cn("mt-2 space-y-2", loading && page && "opacity-60")}>
          {items.map((video) => (isSocialVideo(video) ? <SocialVideoRow key={video.id} video={video} /> : <VideoRow key={video.id} video={video} />))}
        </ul>
        {page?.next_offset !== null && page?.next_offset !== undefined && (
          <div className="mt-4 flex justify-center text-xs">
            <Button variant="outline" size="sm" onClick={() => void loadMore()} disabled={loadingMore}>
              {loadingMore && <LoaderCircle className="size-3.5 animate-spin" />} Load more ({items.length} of {page.total})
            </Button>
          </div>
        )}
      </div>
    </main>
  );
}
