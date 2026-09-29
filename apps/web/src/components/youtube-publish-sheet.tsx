"use client";

import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, CalendarClock, Check, CheckCircle2, ChevronDown, ImagePlus, LoaderCircle, Lock, RefreshCw, RotateCcw, Sparkles, Upload, X } from "lucide-react";
import { ApiError, getNextYouTubeSlot, getProjectYouTube, getPublishingDraft, mediaUrl, preflightYouTubeUpload, retryYouTubeUpload, scheduleYouTubeUpload, uploadCustomThumbnail, uploadProjectToYouTube } from "@/lib/api";
import type { Project } from "@/lib/types";
import {
  attentionSummary,
  audienceLabel,
  browserLocale,
  detectTimeZone,
  formatLocalDate,
  formatScheduleConfirmation,
  parseTags,
  primaryActionLabel,
  publishPhase,
  PARTIAL_VISIBLE_MS,
  STATUS_POLL_MS,
  SUCCESS_VISIBLE_MS,
  regionFromLocale,
  tagsLength,
  textLength,
  utf8Bytes,
  zoneLabel,
  type PreflightIssue,
  type PublishOptions,
  type PublishingDraft,
  type PublishPhase,
  type ScheduleResolution,
  type ThumbnailOption,
  type Visibility,
  type YouTubeUpload,
} from "@/lib/youtube";
import {
  dayContextLine,
  freshnessLabel,
  isManualOverride,
  readRecommendation,
  recommendationLabel,
  slotTakenMessage,
  zoneLine,
  type SlotRecommendation,
  type SmartScheduleState,
} from "@/lib/youtube-schedule";
import { cn } from "@/lib/utils";
import { Button } from "./ui/button";
import { ScheduleFields } from "./youtube-schedule-fields";

const VISIBILITY_COPY: Record<Visibility, { label: string; hint: string }> = {
  private: { label: "Private", hint: "Only you can see it. Publish later." },
  schedule: { label: "Schedule", hint: "Private until the time you choose." },
  unlisted: { label: "Unlisted", hint: "Anyone with the link." },
  public: { label: "Public", hint: "Everyone, right after processing." },
};

function Section({ title, children, hint }: { title: string; hint?: string; children: React.ReactNode }) {
  return (
    <section className="border-t border-[var(--border)] px-5 py-5 first:border-t-0 sm:px-6">
      <h3 className="text-[11px] font-bold uppercase tracking-[.12em] text-[var(--muted-foreground)]">{title}</h3>
      {hint && <p className="mt-1 text-xs text-[var(--muted-foreground)]">{hint}</p>}
      <div className="mt-3">{children}</div>
    </section>
  );
}

function Counter({ value, limit, unit = "" }: { value: number; limit: number; unit?: string }) {
  return <span className={cn("mono text-[10px]", value > limit ? "font-bold text-red-700" : "text-[var(--muted-foreground)]")}>{value}/{limit}{unit}</span>;
}

function Choice({ name, checked, onChange, label, description }: { name: string; checked: boolean; onChange: () => void; label: string; description?: string }) {
  return (
    <label className={cn("flex cursor-pointer items-start gap-3 rounded-xl border px-3 py-2.5 text-sm transition", checked ? "border-[#ff6838] bg-[var(--accent-soft)]" : "border-[var(--border)] hover:border-black/25")}>
      <input type="radio" name={name} checked={checked} onChange={onChange} className="mt-1 accent-[#ff6838]" />
      <span><span className="font-semibold">{label}</span>{description && <span className="block text-xs text-[var(--muted-foreground)]">{description}</span>}</span>
    </label>
  );
}

function Toggle({ label, checked, onChange, hint }: { label: string; checked: boolean; onChange: (value: boolean) => void; hint?: string }) {
  return (
    <label className="flex items-start justify-between gap-3 text-sm">
      <span><span className="font-medium">{label}</span>{hint && <span className="block text-[11px] text-[var(--muted-foreground)]">{hint}</span>}</span>
      <input type="checkbox" role="switch" checked={checked} onChange={(event) => onChange(event.target.checked)} className="mt-1 size-4 accent-[#ff6838]" />
    </label>
  );
}

/** One upload started from this sheet, followed until YouTube answers. */
type Run = { id: string; wantsSchedule: boolean; upload: YouTubeUpload; videoSeenAt: number | null; checkedAt: number; pollErrors: number };

const MAX_POLL_ERRORS = 5;

export function PublishSheet({ project, onClose, onUploaded }: { project: Project; onClose: () => void; onUploaded: () => void }) {
  const locale = browserLocale();
  const region = regionFromLocale(locale);
  const language = locale.split("-")[0] || "en";
  const [draft, setDraft] = useState<PublishingDraft | null>(null);
  const [options, setOptions] = useState<PublishOptions | null>(null);
  const [tagsText, setTagsText] = useState("");
  const [thumbnails, setThumbnails] = useState<ThumbnailOption[]>([]);
  const [issues, setIssues] = useState<PreflightIssue[]>([]);
  const [resolution, setResolution] = useState<ScheduleResolution | null>(null);
  const [checking, setChecking] = useState(false);
  const [advanced, setAdvanced] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [run, setRun] = useState<Run | null>(null);
  // Smart Slot Planner: YouTube's real schedule decides the pre-selected slot.
  const [smart, setSmart] = useState<SmartScheduleState | null>(null);
  const [useCachedSchedule, setUseCachedSchedule] = useState(false);
  const [showTimeFields, setShowTimeFields] = useState(false);
  const [slotNotice, setSlotNotice] = useState<string | null>(null);
  const [checkingSlots, setCheckingSlots] = useState(false);
  const done = useRef(onUploaded);
  useEffect(() => { done.current = onUploaded; });

  useEffect(() => {
    let active = true;
    getPublishingDraft(project.id, region, language, detectTimeZone())
      .then((next) => {
        if (!active) return;
        const timezone = next.smart_schedule?.schedule?.timezone || next.defaults.timezone || detectTimeZone();
        setDraft(next);
        setSmart(next.smart_schedule ?? null);
        setThumbnails(next.thumbnails);
        // The next free slot (verified with YouTube), a time from the last upload, or empty.
        setOptions({ ...next.options, schedule: next.options.schedule ?? { date: "", time: "", timezone } });
        setTagsText(next.options.tags.join(", "));
      })
      .catch((reason) => { if (active) setError(reason instanceof Error ? reason.message : "Publishing settings could not be loaded."); });
    return () => { active = false; };
  }, [project.id, region, language]);

  // The backend is the one settings authority: it re-checks everything.
  useEffect(() => {
    if (!options) return;
    const controller = new AbortController();
    const payload = { ...options, schedule: options.visibility === "schedule" ? options.schedule : null };
    const timer = window.setTimeout(() => {
      setChecking(true);
      preflightYouTubeUpload(project.id, payload, region, language, controller.signal)
        .then((result) => { setIssues(result.issues); setChecking(false); })
        .catch(() => { if (!controller.signal.aborted) setChecking(false); });
    }, 300);
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [options, project.id, region, language]);

  const phase: PublishPhase | null = useMemo(() => {
    if (!run) return null;
    if (run.pollErrors >= MAX_POLL_ERRORS) {
      return { phase: "error", label: "Status unavailable", tone: "error", message: "ClipForge lost contact with the upload. It continues in the background — check the YouTube card after closing.", retry: null, close: false };
    }
    return publishPhase(run.upload, run.wantsSchedule, run.videoSeenAt, run.checkedAt);
  }, [run]);
  const inFlight = busy || phase?.tone === "busy";

  // Follow the upload with the backend's record until YouTube has answered.
  useEffect(() => {
    if (!run || phase?.tone !== "busy") return;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      getProjectYouTube(project.id, controller.signal)
        .then((data) => {
          const found = data.uploads.find((item) => item.id === run.id);
          const now = Date.now();
          setRun((current) => (current && current.id === run.id
            ? { ...current, upload: found ?? current.upload, checkedAt: now, pollErrors: 0, videoSeenAt: current.videoSeenAt ?? (found?.youtube_video_id ? now : null) }
            : current));
        })
        .catch(() => {
          if (controller.signal.aborted) return;
          setRun((current) => (current && current.id === run.id ? { ...current, checkedAt: Date.now(), pollErrors: current.pollErrors + 1 } : current));
        });
    }, STATUS_POLL_MS);
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [run, phase?.tone, project.id]);

  // Success stays visible briefly (and is announced), then the sheet closes itself.
  const closeAfter = phase?.close ? (phase.tone === "success" ? SUCCESS_VISIBLE_MS : PARTIAL_VISIBLE_MS) : null;
  useEffect(() => {
    if (closeAfter === null) return;
    const timer = window.setTimeout(() => done.current(), closeAfter);
    return () => window.clearTimeout(timer);
  }, [closeAfter]);

  const update = (patch: Partial<PublishOptions>) => setOptions((current) => (current ? { ...current, ...patch } : current));
  /** Pre-select a planner slot; "auto" makes the backend re-check and reserve it before upload. */
  const applySlot = (slot: SlotRecommendation, cached = false) => {
    setUseCachedSchedule(cached);
    update({ visibility: "schedule", schedule: { ...slot.choice }, schedule_source: "auto" });
  };
  const selectedThumb = useMemo(
    () => (options?.thumbnail?.source === "youtube_auto" ? null : thumbnails.find((item) => item.source === options?.thumbnail?.source && item.asset === options?.thumbnail?.asset) ?? null),
    [options?.thumbnail, thumbnails],
  );

  if (!draft || !options) {
    return (
      <Shell onClose={onClose}>
        <div className="grid min-h-60 place-items-center p-6 text-sm text-[var(--muted-foreground)]">
          {error ? <p role="alert" className="text-red-700">{error}</p> : <span className="flex items-center gap-2"><LoaderCircle className="size-4 animate-spin" /> Preparing your upload…</span>}
        </div>
      </Shell>
    );
  }

  const limits = draft.limits;
  const audited = draft.allowed_visibilities.includes("public");
  const suggestion = draft.synthetic_suggestion;
  const blocked = issues.length > 0 || checking || inFlight;
  const locked = inFlight || !!phase?.close;

  async function addCustomThumbnail(file: File) {
    setError(null);
    const data = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result));
      reader.onerror = () => reject(new Error("The image could not be read."));
      reader.readAsDataURL(file);
    });
    try {
      const result = await uploadCustomThumbnail(project.id, data);
      setThumbnails(result.thumbnails);
      update({ thumbnail: { source: "custom", asset: result.asset } });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The image could not be used.");
    }
  }

  function follow(upload: YouTubeUpload, wantsSchedule: boolean) {
    const now = Date.now();
    setRun({ id: upload.id, wantsSchedule, upload, videoSeenAt: upload.youtube_video_id ? now : null, checkedAt: now, pollErrors: 0 });
  }

  // The sheet stays open: the button reports progress, then success or an inline error.
  async function submit() {
    if (!options || blocked || locked) return;
    setBusy(true);
    setSubmitError(null);
    setRun(null);
    const wantsSchedule = options.visibility === "schedule";
    const auto = wantsSchedule && options.schedule_source === "auto";
    setSlotNotice(null);
    try {
      const result = await uploadProjectToYouTube(
        project.id, project.current_revision,
        { ...options, schedule: wantsSchedule ? options.schedule : null, schedule_source: wantsSchedule ? options.schedule_source ?? "manual" : null },
        region, language, false, auto && useCachedSchedule,
      );
      follow(result.upload, wantsSchedule);
    } catch (reason) {
      if (reason instanceof ApiError && (reason.code === "slot_taken" || reason.code === "slot_missed")) {
        // Never schedule blindly: show the recalculated slot; the user uploads again.
        const next = readRecommendation(reason.detail);
        if (next) {
          applySlot(next, useCachedSchedule);
          setSmart((current) => (current ? { ...current, recommendation: current.status === "verified" ? next : current.recommendation, cached_recommendation: current.status === "verified" ? current.cached_recommendation : next } : current));
        } else {
          update({ schedule_source: "manual" });
          setShowTimeFields(true);
        }
        setSlotNotice(slotTakenMessage(reason.message, next, new Date(), locale));
      } else if (reason instanceof ApiError && reason.code === "schedule_unverified") {
        const cached = readRecommendation(reason.detail, "cached_recommendation");
        setSmart((current) => (current ? { ...current, status: cached ? "stale_cache" : "unverified", recommendation: null, cached_recommendation: cached, freshness: (reason.detail?.freshness as SmartScheduleState["freshness"]) ?? current.freshness } : current));
        setSlotNotice("Could not verify YouTube schedule. Retry, use the cached schedule, or choose a time manually.");
      } else {
        setSubmitError(`Upload failed. ${reason instanceof ApiError ? reason.message : "The upload could not start."}`);
      }
    } finally {
      setBusy(false);
    }
  }

  async function retry() {
    if (!run || !phase?.retry || busy) return;
    setBusy(true);
    setSubmitError(null);
    try {
      if (phase.retry === "upload") {
        follow((await retryYouTubeUpload(run.id)).upload, run.wantsSchedule);
      } else if (options?.schedule) {
        follow((await scheduleYouTubeUpload(run.id, options.schedule)).upload, true);
      }
    } catch (reason) {
      const text = reason instanceof ApiError ? reason.message : "YouTube could not be reached.";
      setSubmitError(phase.retry === "upload" ? `Upload failed. ${text}` : `Video uploaded, but scheduling failed. ${text}`);
    } finally {
      setBusy(false);
    }
  }

  const summary: Array<[string, string]> = [
    ["Thumbnail", options.thumbnail?.source === "youtube_auto" ? "YouTube's automatic frame" : selectedThumb?.label ?? "Not selected"],
    ["Title", options.title || "—"],
    ["Audience", audienceLabel(options.made_for_kids)],
    ["Visibility", VISIBILITY_COPY[options.visibility].label],
    ["Schedule", options.visibility === "schedule" && resolution?.status === "ok" && resolution.publish_at && options.schedule
      ? `${formatScheduleConfirmation(resolution.publish_at, options.schedule.timezone, locale)} · ${zoneLabel(options.schedule.timezone, resolution)}${options.schedule_source === "auto" ? " · next free slot" : ""}`
      : options.visibility === "schedule" ? "Choose a date and time" : "—"],
    ["Altered or synthetic content", options.contains_synthetic_media === null ? "Not answered" : options.contains_synthetic_media ? "Yes" : "No"],
  ];

  return (
    <Shell onClose={onClose}>
      <div className="min-h-0 flex-1 overflow-y-auto">
        {draft.preset_source === "last_upload" && (
          <p className="mx-5 mt-4 flex gap-1.5 rounded-lg bg-black/[.03] px-3 py-2 text-[11px] text-[var(--muted-foreground)] sm:mx-6">
            <Check className="mt-0.5 size-3 shrink-0 text-[#ff6838]" />Settings from your last upload to this channel are pre-selected. Title, description, tags and thumbnail come from this project.
          </p>
        )}
        {/* Settings stay as entered; they are only read-only while YouTube works. */}
        <fieldset disabled={locked} className="m-0 min-w-0 border-0 p-0">
        <Section title="Video">
          <div className="grid gap-4 md:grid-cols-[210px_minmax(0,1fr)]">
            <div>
              <p className="mb-2 text-xs font-semibold">Thumbnail</p>
              <div className="grid grid-cols-3 gap-1.5" role="radiogroup" aria-label="Thumbnail">
                {thumbnails.map((item) => {
                  const active = options.thumbnail?.source === item.source && options.thumbnail?.asset === item.asset;
                  return (
                    <button key={`${item.source}:${item.asset}`} type="button" role="radio" aria-checked={active} disabled={!item.valid} title={item.problem ?? item.label}
                      onClick={() => update({ thumbnail: { source: item.source, asset: item.asset } })}
                      className={cn("relative overflow-hidden rounded-lg border-2 text-left disabled:opacity-40", active ? "border-[#ff6838] ring-2 ring-[#ff6838]/25" : "border-transparent hover:border-black/20")}>
                      {/* Runtime API media; not routed through next/image. */}
                      {/* eslint-disable-next-line @next/next/no-img-element */}
                      <img src={mediaUrl(item.url) ?? ""} alt={item.label} className="aspect-[9/16] w-full object-cover" />
                      <span className="block truncate px-1 py-0.5 text-[8px] font-bold uppercase tracking-[.04em]">{item.platform ?? "custom"}</span>
                      {active && <span className="absolute right-1 top-1 grid size-5 place-items-center rounded-full bg-[#ff6838] text-white"><Check className="size-3" /></span>}
                    </button>
                  );
                })}
              </div>
              <div className="mt-2 flex flex-col gap-1">
                <label className="interactive-text cursor-pointer text-xs"><ImagePlus className="size-3.5" /> Use your own image
                  <input type="file" accept="image/jpeg,image/png" className="sr-only" onChange={(event) => { const file = event.target.files?.[0]; if (file) void addCustomThumbnail(file); event.target.value = ""; }} />
                </label>
                <label className="flex items-center gap-2 text-[11px] text-[var(--muted-foreground)]">
                  <input type="radio" name="thumbnail-auto" checked={options.thumbnail?.source === "youtube_auto"} onChange={() => update({ thumbnail: { source: "youtube_auto" } })} className="accent-[#ff6838]" /> Let YouTube pick a frame
                </label>
              </div>
            </div>
            <div className="min-w-0 space-y-3">
              <label className="block text-xs font-semibold">
                <span className="flex items-center justify-between">Title <Counter value={textLength(options.title.trim())} limit={limits.title} /></span>
                <input value={options.title} onChange={(event) => update({ title: event.target.value })} className="cf-input mt-1 text-sm" />
              </label>
              <label className="block text-xs font-semibold">
                <span className="flex items-center justify-between">Description <Counter value={utf8Bytes(options.description)} limit={limits.description_bytes} unit=" bytes" /></span>
                <textarea value={options.description} onChange={(event) => update({ description: event.target.value })} className="cf-input mt-1 min-h-28 resize-y text-sm" />
              </label>
              <label className="block text-xs font-semibold">
                <span className="flex items-center justify-between">Tags <Counter value={tagsLength(options.tags)} limit={limits.tags} /></span>
                <input value={tagsText} onChange={(event) => { setTagsText(event.target.value); update({ tags: parseTags(event.target.value) }); }} placeholder="Separate with commas" className="cf-input mt-1 text-sm" />
              </label>
            </div>
          </div>
        </Section>

        <Section title="Audience" hint="YouTube requires this answer for every video. Choose Yes only if the video is made for children.">
          <div className="grid gap-2 sm:grid-cols-2" role="radiogroup" aria-label="Made for kids" aria-required="true">
            <Choice name="made-for-kids" checked={options.made_for_kids === false} onChange={() => update({ made_for_kids: false })} label="No, it's not made for kids" />
            <Choice name="made-for-kids" checked={options.made_for_kids === true} onChange={() => update({ made_for_kids: true })} label="Yes, it's made for kids" description="Comments and some features are turned off." />
          </div>
          {options.made_for_kids !== null && draft.options.made_for_kids !== null && <p className="mt-2 text-[11px] text-[var(--muted-foreground)]">{draft.preset_source === "last_upload" ? "Pre-selected from your last upload." : "Pre-selected from your upload defaults."}</p>}
          <div className="mt-5">
            <p className="text-sm font-semibold">Realistic altered or synthetic content?</p>
            <p className="text-xs text-[var(--muted-foreground)]">Content that could be mistaken for a real person, place or event.</p>
            <div className="mt-2 grid gap-2 sm:grid-cols-2" role="radiogroup" aria-label="Altered or synthetic content" aria-required="true">
              <Choice name="synthetic" checked={options.contains_synthetic_media === false} onChange={() => update({ contains_synthetic_media: false })} label="No" />
              <Choice name="synthetic" checked={options.contains_synthetic_media === true} onChange={() => update({ contains_synthetic_media: true })} label="Yes" description="YouTube adds a disclosure label." />
            </div>
            <p className="mt-2 flex gap-1.5 rounded-lg bg-black/[.03] px-3 py-2 text-[11px]"><Sparkles className="mt-0.5 size-3 shrink-0 text-[#ff6838]" /><span><strong>Suggested: {suggestion.value === null ? "Check the images" : suggestion.value ? "Yes" : "No"}</strong> · Why: {suggestion.why}</span></p>
          </div>
        </Section>

        <Section title="Visibility">
          <div className="grid gap-2 sm:grid-cols-2" role="radiogroup" aria-label="Visibility">
            {draft.allowed_visibilities.map((item) => (
              <Choice key={item} name="visibility" checked={options.visibility === item} onChange={() => update({ visibility: item })} label={VISIBILITY_COPY[item].label} description={VISIBILITY_COPY[item].hint} />
            ))}
          </div>
          {!audited && (
            <p className="mt-2 flex gap-1.5 text-[11px] text-[var(--muted-foreground)]"><Lock className="mt-0.5 size-3 shrink-0" />Public and Unlisted are unavailable: YouTube keeps uploads from unverified Google API projects private. {options.visibility === "schedule" && "A scheduled time takes effect once the project passes YouTube's audit."}</p>
          )}
          {options.visibility === "schedule" && options.schedule && (
            <div className="mt-4 space-y-3">
              {smart && smart.enabled && smart.status !== "not_connected" && (
                <SmartSlot
                  smart={smart}
                  schedule={options.schedule}
                  source={options.schedule_source ?? null}
                  cached={useCachedSchedule}
                  checking={checkingSlots}
                  notice={slotNotice}
                  locale={locale}
                  onApply={applySlot}
                  onManual={() => { update({ schedule_source: "manual" }); setShowTimeFields(true); }}
                  onRetry={() => {
                    setCheckingSlots(true);
                    setSlotNotice(null);
                    getNextYouTubeSlot(true)
                      .then((next) => {
                        setSmart(next);
                        // Only an automatic selection follows the planner; a manual time stays.
                        if (next.recommendation && options.schedule_source !== "manual") applySlot(next.recommendation);
                      })
                      .catch(() => setSlotNotice("Could not verify YouTube schedule."))
                      .finally(() => setCheckingSlots(false));
                  }}
                />
              )}
              {(showTimeFields || options.schedule_source !== "auto" || !smart?.enabled) ? (
                <ScheduleFields
                  value={options.schedule}
                  // A manual edit is respected for this upload and never snapped back.
                  onChange={(schedule) => update({ schedule, schedule_source: "manual" })}
                  locale={locale}
                  onResolved={setResolution}
                />
              ) : (
                <>
                  <ScheduleFields value={options.schedule} onChange={() => undefined} locale={locale} onResolved={setResolution} hidden />
                  <button type="button" className="interactive-text text-[11px]" onClick={() => setShowTimeFields(true)}><CalendarClock className="size-3" /> Change date or time</button>
                </>
              )}
            </div>
          )}
        </Section>

        <section className="border-t border-[var(--border)] px-5 py-4 sm:px-6">
          <button type="button" className="flex w-full items-center justify-between text-[11px] font-bold uppercase tracking-[.12em] text-[var(--muted-foreground)]" aria-expanded={advanced} onClick={() => setAdvanced((open) => !open)}>
            Advanced settings <ChevronDown className={cn("size-4 transition-transform", advanced && "rotate-180")} />
          </button>
          {advanced && (
            <div className="mt-4 grid gap-4 sm:grid-cols-2">
              <label className="text-xs font-semibold">Category
                <select value={options.category_id ?? ""} onChange={(event) => update({ category_id: event.target.value || null })} className="cf-input mt-1 text-sm">
                  <option value="">YouTube default</option>
                  {draft.categories.map((item) => <option key={item.id} value={item.id}>{item.title}</option>)}
                </select>
                {draft.suggested_category && options.category_id !== draft.suggested_category.id && (
                  <button type="button" className="mt-1 text-[11px] text-[#d94c20]" onClick={() => update({ category_id: draft.suggested_category!.id })}>Suggested: {draft.suggested_category.title}</button>
                )}
                {draft.category_error && <span className="mt-1 block text-[11px] font-normal text-amber-800">Categories unavailable: {draft.category_error.message}</span>}
              </label>
              <label className="text-xs font-semibold">Video language
                <input value={options.default_language ?? ""} onChange={(event) => update({ default_language: event.target.value.trim() || null, default_audio_language: event.target.value.trim() || null })} placeholder="e.g. en, de" className="cf-input mt-1 text-sm" />
              </label>
              <fieldset className="text-xs">
                <legend className="font-semibold">License</legend>
                <label className="mt-1 flex items-center gap-2"><input type="radio" name="license" checked={options.license === "youtube"} onChange={() => update({ license: "youtube" })} className="accent-[#ff6838]" /> Standard YouTube License</label>
                <label className="mt-1 flex items-center gap-2"><input type="radio" name="license" checked={options.license === "creativeCommon"} onChange={() => update({ license: "creativeCommon" })} className="accent-[#ff6838]" /> Creative Commons – Attribution</label>
              </fieldset>
              <label className="text-xs font-semibold">Recording date
                <input type="date" value={options.recording_date ?? ""} onChange={(event) => update({ recording_date: event.target.value || null })} className="cf-input mt-1 text-sm" />
              </label>
              <div className="space-y-3 sm:col-span-2">
                <Toggle label="Allow embedding" checked={options.embeddable} onChange={(embeddable) => update({ embeddable })} />
                <Toggle label="Show public statistics" hint="Extended statistics on the watch page." checked={options.public_stats_viewable} onChange={(value) => update({ public_stats_viewable: value })} />
                <Toggle label="Notify subscribers" checked={options.notify_subscribers} onChange={(value) => update({ notify_subscribers: value })} />
                <Toggle label="Contains paid promotion" hint="Product placement, sponsorship or endorsement." checked={options.paid_product_placement} onChange={(value) => update({ paid_product_placement: value })} />
              </div>
              <div className="rounded-xl bg-black/[.03] px-3 py-2 text-[11px] text-[var(--muted-foreground)] sm:col-span-2">
                <p className="font-semibold text-[var(--foreground)]">Additional YouTube Studio settings</p>
                <p className="mt-0.5">Not available through YouTube&apos;s public API — set them in YouTube Studio after the upload: {draft.catalog.studio_only.map((item) => item.label).join(", ")}.</p>
              </div>
            </div>
          )}
        </section>
        </fieldset>
      </div>

      <footer className="border-t border-[var(--border)] bg-[var(--surface-elevated)] px-5 py-4 sm:px-6">
        <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-[11px] sm:grid-cols-3">
          {summary.map(([label, value]) => <div key={label} className="min-w-0"><dt className="text-[var(--muted-foreground)]">{label}</dt><dd className="truncate font-semibold" title={value}>{value}</dd></div>)}
        </dl>
        {issues.length > 0 && !run && (
          <div role="alert" className="mt-3 rounded-xl border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900">
            <p className="flex items-center gap-1.5 font-semibold"><AlertTriangle className="size-3.5" /> {attentionSummary(issues)}</p>
            <ul className="mt-1 list-disc pl-5">{issues.map((item) => <li key={`${item.field}:${item.message}`}>{item.message}</li>)}</ul>
          </div>
        )}
        {error && <p role="alert" className="mt-3 rounded-xl bg-red-50 px-3 py-2 text-xs text-red-800">{error}</p>}
        <PublishFeedback phase={phase} submitError={submitError} />
        <div className="mt-3 flex items-center justify-end gap-2">
          <Button variant="ghost" onClick={onClose} disabled={busy}>{run ? "Close" : "Cancel"}</Button>
          <PrimaryAction
            phase={busy && !run ? { phase: "uploading", label: "Uploading…", tone: "busy", message: null, retry: null, close: false } : busy && phase?.retry ? { ...phase, tone: "busy", label: phase.retry === "schedule" ? "Scheduling…" : "Uploading…" } : phase}
            idleLabel={submitError && !run ? "Retry upload" : primaryActionLabel(options.visibility)}
            disabled={blocked}
            onSubmit={() => void submit()}
            onRetry={() => void retry()}
          />
        </div>
        {inFlight && run && <p className="mt-1 text-right text-[10px] text-[var(--muted-foreground)]">You can close this — the upload continues in the background.</p>}
        {blocked && !inFlight && !phase && <p id="publish-blocked-reason" className="mt-1 text-right text-[10px] text-[var(--muted-foreground)]">{checking ? "Checking settings…" : "Resolve the items above to upload."}</p>}
      </footer>
    </Shell>
  );
}

/**
 * "Schedule automatically": the next free slot from the channel's real YouTube
 * schedule, why it was chosen, and how fresh that knowledge is. Transparent:
 * the actual date and time are always shown; staleness is never hidden.
 */
function SmartSlot({ smart, schedule, source, cached, checking, notice, locale, onApply, onManual, onRetry }: {
  smart: SmartScheduleState;
  schedule: { date: string; time: string; timezone: string };
  source: "auto" | "manual" | null;
  cached: boolean;
  checking: boolean;
  notice: string | null;
  locale: string;
  onApply: (slot: SlotRecommendation, cached?: boolean) => void;
  onManual: () => void;
  onRetry: () => void;
}) {
  const now = new Date();
  const recommended = smart.recommendation ?? null;
  const fallback = smart.cached_recommendation ?? null;
  const shown = recommended ?? (cached ? fallback : null);
  const manual = source === "manual" || (shown !== null && isManualOverride(schedule, shown));
  const unverified = smart.status === "unverified" || smart.status === "stale_cache";
  return (
    <div className="rounded-xl border border-[var(--border)] bg-[var(--surface-elevated)] px-3 py-2.5 text-xs" aria-label="Recommended schedule" data-smart-status={smart.status}>
      <div className="flex items-start justify-between gap-2">
        <p className="flex items-center gap-1.5 font-semibold"><CalendarClock className="size-3.5 text-[#ff6838]" /> {manual ? "Your time" : "Schedule automatically"}</p>
        <button type="button" className="interactive-text text-[11px]" onClick={onRetry} disabled={checking}>{checking ? <LoaderCircle className="size-3 animate-spin" /> : <RefreshCw className="size-3" />} {unverified ? "Retry" : "Refresh schedule"}</button>
      </div>
      {shown && !manual && (
        <>
          <p className="mt-1">Recommended: <strong className="text-sm" data-testid="recommended-slot">{recommendationLabel(shown, now, locale)}</strong> <span className="text-[var(--muted-foreground)]">({formatLocalDate(shown.local_date, locale)})</span></p>
          <p className="text-[11px] text-[var(--muted-foreground)]">{zoneLine(shown.timezone, shown.abbreviation)}</p>
          <p className="mt-1 text-[11px]">Why: {shown.reason}</p>
          <p className="mt-0.5 text-[11px] text-[var(--muted-foreground)]">{dayContextLine(shown, now, locale)}</p>
        </>
      )}
      {manual && (
        <p className="mt-1 text-[11px] text-[var(--muted-foreground)]">
          Your chosen time is used for this upload.
          {shown && <> Next available slot: {recommendationLabel(shown, now, locale)}. <button type="button" className="underline" onClick={() => onApply(shown, !recommended)}>Use it</button></>}
        </p>
      )}
      {unverified && !cached && (
        <div role="alert" className="mt-2 rounded-lg bg-amber-50 px-2.5 py-2 text-[11px] text-amber-900">
          <p className="flex items-center gap-1.5 font-semibold"><AlertTriangle className="size-3" /> Could not verify YouTube schedule.</p>
          <p className="mt-0.5">{smart.freshness?.error?.message ?? "YouTube did not answer."} ClipForge will not assume a slot is free.</p>
          <div className="mt-1.5 flex flex-wrap gap-3">
            <button type="button" className="underline" onClick={onRetry} disabled={checking}>Retry</button>
            {fallback && <button type="button" className="underline" onClick={() => onApply(fallback, true)}>Use cached schedule ({freshnessLabel(smart.freshness, true).replace("Based on schedule checked ", "checked ")})</button>}
            <button type="button" className="underline" onClick={onManual}>Choose time manually</button>
          </div>
        </div>
      )}
      {cached && shown && !manual && <p className="mt-1 text-[11px] font-semibold text-amber-800">{freshnessLabel(smart.freshness, true)} — YouTube was not re-checked.</p>}
      {!unverified && smart.freshness && <p className="mt-1 text-[10px] text-[var(--muted-foreground)]">{freshnessLabel(smart.freshness)}</p>}
      {smart.horizon_full && <p className="mt-1 text-[11px] text-amber-800">Every slot in your schedule is taken for the next {smart.schedule?.horizon_days ?? 30} days — choose a time manually.</p>}
      {notice && <p role="status" className="mt-2 rounded-lg bg-amber-50 px-2.5 py-1.5 text-[11px] font-semibold text-amber-900">{notice}</p>}
    </div>
  );
}

const PHASE_TONE: Record<PublishPhase["tone"], string> = {
  busy: "!opacity-90",
  success: "!bg-emerald-600 !text-white !opacity-100",
  warn: "!bg-amber-500 !text-white !opacity-100",
  error: "",
};

/** The one primary button: Upload → Uploading… → Scheduling… → Uploaded ✓ / Scheduled ✓ (or Retry …). */
function PrimaryAction({ phase, idleLabel, disabled, onSubmit, onRetry }: { phase: PublishPhase | null; idleLabel: string; disabled: boolean; onSubmit: () => void; onRetry: () => void }) {
  if (!phase) {
    return (
      <Button variant="accent" onClick={onSubmit} disabled={disabled} aria-describedby="publish-blocked-reason">
        <Upload className="size-3.5" /> {idleLabel}
      </Button>
    );
  }
  if (phase.retry && phase.tone !== "busy") {
    return <Button variant="accent" onClick={onRetry}><RotateCcw className="size-3.5" /> {phase.label}</Button>;
  }
  const finished = phase.close;
  return (
    <Button variant="accent" disabled aria-disabled="true" aria-busy={phase.tone === "busy"} data-phase={phase.phase}
      className={cn("transition-colors duration-300", PHASE_TONE[phase.tone])}>
      {phase.tone === "busy" ? <LoaderCircle className="size-3.5 animate-spin" /> : finished ? <CheckCircle2 className="size-3.5" /> : <AlertTriangle className="size-3.5" />} {phase.label}
    </Button>
  );
}

/** Inline, screen-reader-announced outcome next to the button. */
function PublishFeedback({ phase, submitError }: { phase: PublishPhase | null; submitError: string | null }) {
  const problem = submitError ?? (phase && !phase.close && phase.tone !== "busy" ? phase.message : null);
  const note = !submitError && phase?.close ? phase.message : null;
  return (
    <>
      <p role="status" aria-live="polite" className="sr-only">{phase && !submitError ? [phase.label, phase.message].filter(Boolean).join(". ") : ""}</p>
      {problem && <p role="alert" className="mt-3 rounded-xl bg-red-50 px-3 py-2 text-xs text-red-800">{problem}</p>}
      {note && phase?.tone === "warn" && <p className="mt-3 rounded-xl bg-amber-50 px-3 py-2 text-xs text-amber-900">{note}</p>}
    </>
  );
}

function Shell({ children, onClose }: { children: React.ReactNode; onClose: () => void }) {
  return (
    <div className="fixed inset-0 z-[80] flex justify-end bg-black/40" role="dialog" aria-modal="true" aria-labelledby="publish-sheet-title">
      <div className="flex h-full w-full max-w-[760px] flex-col border-l border-[var(--border)] bg-[var(--background)] shadow-[0_0_80px_rgba(0,0,0,.25)]">
        <header className="flex items-center justify-between border-b border-[var(--border)] px-5 py-4 sm:px-6">
          <div>
            <h2 id="publish-sheet-title" className="text-lg font-semibold tracking-[-.02em]">Upload to YouTube</h2>
            <p className="text-xs text-[var(--muted-foreground)]">Straight from ClipForge — no export needed. <Link href="/settings/integrations#youtube" className="underline">Upload defaults</Link></p>
          </div>
          <button type="button" className="interactive-icon" onClick={onClose} aria-label="Close"><X className="size-4" /></button>
        </header>
        {children}
      </div>
    </div>
  );
}
