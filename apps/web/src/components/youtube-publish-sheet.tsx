"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { AlertTriangle, Check, ChevronDown, ImagePlus, LoaderCircle, Lock, Sparkles, Upload, X } from "lucide-react";
import { ApiError, getPublishingDraft, mediaUrl, preflightYouTubeUpload, uploadCustomThumbnail, uploadProjectToYouTube } from "@/lib/api";
import type { Project } from "@/lib/types";
import {
  attentionSummary,
  audienceLabel,
  browserLocale,
  detectTimeZone,
  formatScheduleConfirmation,
  parseTags,
  primaryActionLabel,
  regionFromLocale,
  tagsLength,
  textLength,
  utf8Bytes,
  zoneLabel,
  type PreflightIssue,
  type PublishOptions,
  type PublishingDraft,
  type ScheduleResolution,
  type ThumbnailOption,
  type Visibility,
} from "@/lib/youtube";
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

  useEffect(() => {
    let active = true;
    getPublishingDraft(project.id, region, language)
      .then((next) => {
        if (!active) return;
        const timezone = next.defaults.timezone || detectTimeZone();
        setDraft(next);
        setThumbnails(next.thumbnails);
        setOptions({ ...next.options, schedule: { date: "", time: "", timezone } });
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

  const update = (patch: Partial<PublishOptions>) => setOptions((current) => (current ? { ...current, ...patch } : current));
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
  const blocked = issues.length > 0 || checking || busy;

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

  async function submit() {
    if (!options || blocked) return;
    setBusy(true);
    setError(null);
    try {
      await uploadProjectToYouTube(project.id, project.current_revision, { ...options, schedule: options.visibility === "schedule" ? options.schedule : null }, region, language);
      onUploaded();
    } catch (reason) {
      if (reason instanceof ApiError) setError(reason.message);
      else setError("The upload could not start.");
      setBusy(false);
    }
  }

  const summary: Array<[string, string]> = [
    ["Thumbnail", options.thumbnail?.source === "youtube_auto" ? "YouTube's automatic frame" : selectedThumb?.label ?? "Not selected"],
    ["Title", options.title || "—"],
    ["Audience", audienceLabel(options.made_for_kids)],
    ["Visibility", VISIBILITY_COPY[options.visibility].label],
    ["Schedule", options.visibility === "schedule" && resolution?.status === "ok" && resolution.publish_at && options.schedule
      ? `${formatScheduleConfirmation(resolution.publish_at, options.schedule.timezone, locale)} · ${zoneLabel(options.schedule.timezone, resolution)}`
      : options.visibility === "schedule" ? "Choose a date and time" : "—"],
    ["Altered or synthetic content", options.contains_synthetic_media === null ? "Not answered" : options.contains_synthetic_media ? "Yes" : "No"],
  ];

  return (
    <Shell onClose={onClose}>
      <div className="min-h-0 flex-1 overflow-y-auto">
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
          {draft.defaults.made_for_kids !== null && <p className="mt-2 text-[11px] text-[var(--muted-foreground)]">Pre-selected from your upload defaults.</p>}
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
            <div className="mt-4">
              <ScheduleFields value={options.schedule} onChange={(schedule) => update({ schedule })} locale={locale} onResolved={setResolution} />
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
      </div>

      <footer className="border-t border-[var(--border)] bg-[var(--surface-elevated)] px-5 py-4 sm:px-6">
        <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-[11px] sm:grid-cols-3">
          {summary.map(([label, value]) => <div key={label} className="min-w-0"><dt className="text-[var(--muted-foreground)]">{label}</dt><dd className="truncate font-semibold" title={value}>{value}</dd></div>)}
        </dl>
        {issues.length > 0 && (
          <div role="alert" className="mt-3 rounded-xl border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900">
            <p className="flex items-center gap-1.5 font-semibold"><AlertTriangle className="size-3.5" /> {attentionSummary(issues)}</p>
            <ul className="mt-1 list-disc pl-5">{issues.map((item) => <li key={`${item.field}:${item.message}`}>{item.message}</li>)}</ul>
          </div>
        )}
        {error && <p role="alert" className="mt-3 rounded-xl bg-red-50 px-3 py-2 text-xs text-red-800">{error}</p>}
        <div className="mt-3 flex items-center justify-end gap-2">
          <Button variant="ghost" onClick={onClose} disabled={busy}>Cancel</Button>
          <Button variant="accent" onClick={() => void submit()} disabled={blocked} aria-describedby="publish-blocked-reason">
            {busy ? <LoaderCircle className="size-3.5 animate-spin" /> : <Upload className="size-3.5" />} {primaryActionLabel(options.visibility)}
          </Button>
        </div>
        {blocked && !busy && <p id="publish-blocked-reason" className="mt-1 text-right text-[10px] text-[var(--muted-foreground)]">{checking ? "Checking settings…" : "Resolve the items above to upload."}</p>}
      </footer>
    </Shell>
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
