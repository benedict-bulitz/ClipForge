"use client";

import { useEffect, useMemo, useState } from "react";
import { AlertTriangle, CalendarClock, Check, LoaderCircle, RefreshCw, RotateCcw, Save, Sparkles } from "lucide-react";
import { ApiError, applyLearnedYouTubeSchedule, getYouTubeSchedule, refreshYouTubeSchedule, saveYouTubeSchedule } from "@/lib/api";
import { browserLocale, detectTimeZone, zoneLabel } from "@/lib/youtube";
import {
  VIDEOS_PER_DAY_OPTIONS,
  dayLabel,
  formatNormalized,
  freshnessLabel,
  isOccupied,
  learningStatusText,
  scheduleLoadError,
  recommendationLabel,
  scheduleModeHint,
  scheduleModeLabel,
  slotStatusLabel,
  slotsForCount,
  timeOptions,
  validateSlotDraft,
  zoneLine,
  type ScheduleOverview,
  type ScheduleUpdate,
} from "@/lib/youtube-schedule";
import { cn } from "@/lib/utils";
import { Alert } from "./ui/alert";
import { Button } from "./ui/button";
import { timeZones } from "./youtube-schedule-fields";

const TIMES = timeOptions(5);
const TOLERANCES = [0, 15, 30, 45, 60, 90, 120];
const LEADS = [15, 30, 60, 120];

function toUpdate(overview: ScheduleOverview): ScheduleUpdate | null {
  const schedule = overview.schedule;
  if (!schedule) return null;
  return {
    videos_per_day: schedule.videos_per_day,
    timezone: schedule.timezone,
    slots: [...schedule.slots],
    enabled: schedule.enabled,
    occupancy_tolerance_minutes: schedule.occupancy_tolerance_minutes,
    min_lead_minutes: schedule.min_lead_minutes,
  };
}

/**
 * Settings → Integrations → YouTube → Publishing schedule: the channel's
 * cadence, its preferred slots, what is really on the channel (checked with
 * YouTube) and - only once enough data exists - a learned proposal to approve.
 */
export function YouTubePublishingSchedule() {
  const locale = browserLocale();
  const [overview, setOverview] = useState<ScheduleOverview | null>(null);
  const [draft, setDraft] = useState<ScheduleUpdate | null>(null);
  const [busy, setBusy] = useState<"save" | "refresh" | "learned" | null>(null);
  const [notice, setNotice] = useState<{ tone: "ok" | "error"; text: string } | null>(null);
  const [now, setNow] = useState(() => new Date());
  const [loadError, setLoadError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  function accept(next: ScheduleOverview) {
    setOverview(next);
    setDraft(toUpdate(next));
    setNow(new Date());
  }

  useEffect(() => {
    let active = true;
    getYouTubeSchedule(detectTimeZone())
      .then((next) => {
        if (!active) return;
        setLoadError(null);
        setOverview(next);
        setDraft(toUpdate(next));
        setNow(new Date());
      })
      .catch((reason) => {
        // A first-use channel gets a default schedule from the backend; only a real failure lands here.
        if (active) setLoadError(scheduleLoadError(reason instanceof ApiError ? reason.message : null));
      });
    return () => { active = false; };
  }, [attempt]);

  const zones = useMemo(() => timeZones(draft?.timezone ?? detectTimeZone()), [draft?.timezone]);
  if (!overview || !draft || !overview.schedule) {
    if (loadError) {
      return (
        <Alert tone="error" className="mt-4 items-center" action={<Button size="sm" variant="outline" onClick={() => { setLoadError(null); setAttempt((value) => value + 1); }}><RefreshCw className="size-3.5" /> Retry</Button>}>
          {loadError}
        </Alert>
      );
    }
    return <p className="mt-4 text-xs text-[var(--muted-foreground)]">Loading publishing schedule…</p>;
  }
  const schedule = overview.schedule;
  const presets = schedule.seed_presets;
  const { errors, warnings } = validateSlotDraft(draft.slots, draft.videos_per_day);
  const dirty = JSON.stringify(draft) !== JSON.stringify(toUpdate(overview));
  const isSeed = draft.slots.join() === (presets[String(draft.videos_per_day)] ?? []).join();
  const modeShown = dirty ? (isSeed ? "seed" : "manual") : schedule.mode;
  const set = (patch: Partial<ScheduleUpdate>) => setDraft((current) => (current ? { ...current, ...patch } : current));
  const freshness = overview.freshness;
  const learning = overview.learning;

  async function run(kind: "save" | "refresh" | "learned", action: () => Promise<ScheduleOverview>, success: string) {
    setBusy(kind);
    setNotice(null);
    try {
      accept(await action());
      setNotice({ tone: "ok", text: success });
    } catch (reason) {
      setNotice({ tone: "error", text: reason instanceof ApiError ? reason.message : "The publishing schedule could not be updated." });
    } finally {
      setBusy(null);
    }
  }

  return (
    <div id="publishing-schedule" className="cf-subtle mt-4 rounded-[15px] border p-3 text-xs" aria-label="Publishing schedule">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="flex items-center gap-1.5 text-[10px] font-bold uppercase tracking-[.1em] text-[var(--muted-foreground)]"><CalendarClock className="size-3" /> Publishing schedule</p>
        <span className="rounded-full bg-black/[.05] px-2 py-0.5 text-[10px] font-semibold" data-mode={modeShown}>{scheduleModeLabel(modeShown, modeShown === "learned" ? schedule.learned_sample_size : null)}</span>
      </div>
      <p className="mt-1 text-[11px] text-[var(--muted-foreground)]">{scheduleModeHint(modeShown)} ClipForge checks your real YouTube schedule and pre-selects the next free slot when you upload.</p>

      <fieldset className="mt-3">
        <legend className="text-[11px] font-semibold">Videos per day</legend>
        <div className="mt-1 inline-flex rounded-xl border border-[var(--border)] p-0.5" role="radiogroup" aria-label="Videos per day">
          {VIDEOS_PER_DAY_OPTIONS.map((count) => (
            <button key={count} type="button" role="radio" aria-checked={draft.videos_per_day === count}
              onClick={() => set({ videos_per_day: count, slots: slotsForCount(count, presets) })}
              className={cn("min-w-9 rounded-[10px] px-2.5 py-1 text-xs font-semibold", draft.videos_per_day === count ? "bg-[#ff6838] text-white" : "hover:bg-black/[.05]")}>
              {count}
            </button>
          ))}
        </div>
      </fieldset>

      <div className="mt-3">
        <p className="text-[11px] font-semibold">Slots <span className="font-normal text-[var(--muted-foreground)]">· {zoneLine(draft.timezone, overview.days?.[0]?.slots.find((slot) => slot.abbreviation)?.abbreviation)}</span></p>
        <div className="mt-1 flex flex-wrap gap-1.5">
          {draft.slots.map((slot, index) => (
            <select key={index} aria-label={`Slot ${index + 1}`} value={slot} className="cf-input w-auto text-xs"
              onChange={(event) => set({ slots: draft.slots.map((item, position) => (position === index ? event.target.value : item)) })}>
              {(TIMES.includes(slot) ? TIMES : [slot, ...TIMES]).map((item) => <option key={item} value={item}>{item}</option>)}
            </select>
          ))}
          {!isSeed && (
            <button type="button" className="interactive-text text-[11px]" onClick={() => set({ slots: slotsForCount(draft.videos_per_day, presets) })}><RotateCcw className="size-3" /> Starter times</button>
          )}
        </div>
        {errors.map((item) => <p key={item} role="alert" className="cf-text-error mt-1 text-[11px]">{item}</p>)}
        {warnings.map((item) => <p key={item} className="cf-text-warning mt-1 text-[11px]">{item}</p>)}
      </div>

      <div className="mt-3 grid gap-3 sm:grid-cols-2">
        <label className="text-[11px] font-semibold">Time zone
          <select value={draft.timezone} onChange={(event) => set({ timezone: event.target.value })} className="cf-input mt-1 text-xs">
            {zones.map((zone) => <option key={zone} value={zone}>{zone}</option>)}
          </select>
          <span className="mt-0.5 block font-normal text-[var(--muted-foreground)]">{zoneLabel(draft.timezone)}</span>
        </label>
        <label className="flex items-start justify-between gap-2 pt-4">
          <span><span className="font-semibold">Schedule automatically</span><span className="block text-[11px] text-[var(--muted-foreground)]">Pre-select the next free slot in Upload to YouTube. You can always change it per video.</span></span>
          <input type="checkbox" role="switch" checked={draft.enabled} onChange={(event) => set({ enabled: event.target.checked })} className="mt-1 accent-[#ff6838]" />
        </label>
        <label className="text-[11px] font-semibold">A video near a slot occupies it within
          <select value={draft.occupancy_tolerance_minutes} onChange={(event) => set({ occupancy_tolerance_minutes: Number(event.target.value) })} className="cf-input mt-1 text-xs">
            {TOLERANCES.map((value) => <option key={value} value={value}>±{value} min</option>)}
          </select>
        </label>
        <label className="text-[11px] font-semibold">Minimum lead time
          <select value={draft.min_lead_minutes} onChange={(event) => set({ min_lead_minutes: Number(event.target.value) })} className="cf-input mt-1 text-xs">
            {LEADS.map((value) => <option key={value} value={value}>{value} min</option>)}
          </select>
        </label>
      </div>
      <div className="mt-3 flex items-center justify-end gap-2">
        {dirty && <Button size="sm" variant="ghost" onClick={() => setDraft(toUpdate(overview))}>Discard</Button>}
        <Button size="sm" variant="outline" disabled={!dirty || errors.length > 0 || !!busy} onClick={() => void run("save", () => saveYouTubeSchedule(draft), "Publishing schedule saved.")}>
          {busy === "save" ? <LoaderCircle className="size-3.5 animate-spin" /> : <Save className="size-3.5" />} Save schedule
        </Button>
      </div>

      <div className="mt-4 border-t border-[var(--border)] pt-3" aria-label="Upcoming schedule">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="text-[11px] font-semibold">{schedule.videos_per_day} video{schedule.videos_per_day === 1 ? "" : "s"}/day · upcoming</p>
          <button type="button" className="interactive-text text-[11px]" disabled={!!busy} onClick={() => void run("refresh", refreshYouTubeSchedule, "Checked with YouTube.")}>
            {busy === "refresh" ? <LoaderCircle className="size-3 animate-spin" /> : <RefreshCw className="size-3" />} Refresh schedule
          </button>
        </div>
        <p className={cn("mt-0.5 text-[10px]", freshness?.error ? "cf-text-warning" : "text-[var(--muted-foreground)]")}>
          {freshness?.error ? <><AlertTriangle className="mr-1 inline size-3" />Could not verify YouTube schedule ({freshness.error.message}). {freshnessLabel(freshness, true)}.</> : freshnessLabel(freshness)}
        </p>
        {overview.recommendation && <p className="mt-1 text-[11px]">Next free slot: <strong>{recommendationLabel(overview.recommendation, now, locale)}</strong></p>}
        {overview.horizon_full && <p className="cf-text-warning mt-1 text-[11px]">Every slot in the next {schedule.horizon_days} days is taken.</p>}
        <div className="mt-2 grid gap-2 sm:grid-cols-2">
          {(overview.days ?? []).slice(0, 4).map((day) => (
            <div key={day.date} className="rounded-xl border border-[var(--border)] px-2.5 py-2">
              <p className="flex justify-between text-[11px] font-semibold"><span>{dayLabel(day.date, schedule.timezone, now, locale)}</span><span className="font-normal text-[var(--muted-foreground)]">{day.count}/{day.target}</span></p>
              <ul className="mt-1 space-y-0.5">
                {day.slots.map((slot) => (
                  <li key={`${day.date}-${slot.position}`} className="flex justify-between gap-2 text-[11px]" data-status={slot.status}>
                    <span className="mono">{slot.local_time}</span>
                    <span className={cn(isOccupied(slot.status) ? "cf-text-success font-semibold" : slot.status === "free" ? "text-[var(--foreground)]" : "text-[var(--muted-foreground)]")}>{slotStatusLabel(slot.status)}</span>
                  </li>
                ))}
                {day.other.map((item) => (
                  <li key={`${day.date}-${item.at}`} className="flex justify-between gap-2 text-[10px] text-[var(--muted-foreground)]">
                    <span className="mono">{new Intl.DateTimeFormat(locale, { hour: "2-digit", minute: "2-digit", hourCycle: "h23", timeZone: schedule.timezone }).format(new Date(item.at))}</span>
                    <span>{slotStatusLabel(item.kind as "published" | "scheduled" | "reserved")} · outside your slots</span>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>
      </div>

      <div className="mt-4 border-t border-[var(--border)] pt-3" aria-label="Learned schedule">
        <p className="flex items-center gap-1.5 text-[11px] font-semibold"><Sparkles className="size-3 text-[#ff6838]" /> Learned schedule</p>
        <p className="mt-0.5 text-[11px] text-[var(--muted-foreground)]" data-learning={learning.available ? "available" : "unavailable"}>{learningStatusText(learning)}</p>
        {learning.available && learning.suggested && (
          <div className="mt-2 rounded-xl bg-black/[.03] px-3 py-2 text-[11px]">
            <p>Current: <span className="mono">{learning.current.join(" · ")}</span></p>
            <p>Suggested from channel data: <span className="mono font-semibold">{learning.suggested.join(" · ")}</span></p>
            <p className="text-[var(--muted-foreground)]">Based on: {learning.based_on} eligible Shorts · {learning.metric}</p>
            {learning.differs && (
              <Button size="sm" variant="outline" className="mt-2" disabled={!!busy} onClick={() => void run("learned", applyLearnedYouTubeSchedule, "Learned schedule applied.")}>
                {busy === "learned" ? <LoaderCircle className="size-3.5 animate-spin" /> : <Check className="size-3.5" />} Apply learned schedule
              </Button>
            )}
          </div>
        )}
        {learning.available && learning.windows.length > 0 && (
          <table className="mt-2 w-full text-[11px]" aria-label="Slot performance">
            <thead><tr className="text-left text-[var(--muted-foreground)]"><th className="font-normal">Window</th><th className="font-normal">n</th><th className="font-normal">Median vs channel</th></tr></thead>
            <tbody>
              {learning.windows.map((item) => (
                <tr key={item.window}><td className="mono">{item.window}</td><td>n={item.n}</td><td>{item.sufficient ? formatNormalized(item.median_normalized) : "too few videos"}</td></tr>
              ))}
            </tbody>
          </table>
        )}
        {learning.available && <p className="mt-1 text-[10px] text-[var(--muted-foreground)]">{learning.caveat}</p>}
      </div>

      {notice && <Alert tone={notice.tone === "error" ? "error" : "success"} size="sm" className="mt-3">{notice.text}</Alert>}
    </div>
  );
}
