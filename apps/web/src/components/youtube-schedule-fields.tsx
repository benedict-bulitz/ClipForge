"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { CalendarClock } from "lucide-react";
import { resolveYouTubeSchedule } from "@/lib/api";
import {
  formatLocalDate,
  formatScheduleConfirmation,
  uses24HourClock,
  zoneLabel,
  type ScheduleChoice,
  type ScheduleResolution,
} from "@/lib/youtube";
import { cn } from "@/lib/utils";

const HOURS = Array.from({ length: 24 }, (_, index) => String(index).padStart(2, "0"));
const MINUTES = Array.from({ length: 12 }, (_, index) => String(index * 5).padStart(2, "0"));

function hourLabel(hour: string, locale: string, twentyFour: boolean): string {
  if (twentyFour) return hour;
  return new Intl.DateTimeFormat(locale, { hour: "numeric", hourCycle: "h12", timeZone: "UTC" }).format(new Date(Date.UTC(2026, 0, 1, Number(hour))));
}

export function timeZones(current: string): string[] {
  const supported = (Intl as unknown as { supportedValuesOf?: (key: string) => string[] }).supportedValuesOf?.("timeZone") ?? [];
  return Array.from(new Set([current, ...supported, "UTC"])).sort((a, b) => (a === current ? -1 : b === current ? 1 : a.localeCompare(b)));
}

/**
 * Date, time and an explicit IANA time zone. The backend resolves the exact
 * instant with the tz database (DST-safe) and the result is shown in words.
 */
export function ScheduleFields({ value, onChange, locale, onResolved }: {
  value: ScheduleChoice;
  onChange: (value: ScheduleChoice) => void;
  locale: string;
  onResolved?: (resolution: ScheduleResolution | null) => void;
}) {
  const [resolution, setResolution] = useState<ScheduleResolution | null>(null);
  const zones = useMemo(() => timeZones(value.timezone), [value.timezone]);
  const twentyFour = uses24HourClock(locale);
  const [hour, minute] = value.time ? value.time.split(":") : ["", ""];
  const notify = useRef(onResolved);
  useEffect(() => { notify.current = onResolved; }, [onResolved]);

  useEffect(() => {
    if (!value.date || !value.time) return;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      resolveYouTubeSchedule(value, controller.signal)
        .then((next) => { setResolution(next); notify.current?.(next); })
        .catch(() => { if (!controller.signal.aborted) { setResolution(null); notify.current?.(null); } });
    }, 250);
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [value]);

  const shown = value.date && value.time ? resolution : null;
  const ok = shown?.status === "ok" && shown.publish_at;
  return (
    <div className="space-y-3">
      <div className="grid gap-2 sm:grid-cols-[1fr_1fr_1.6fr]">
        <label className="text-[11px] font-semibold">Date
          <input type="date" required value={value.date} onChange={(event) => onChange({ ...value, date: event.target.value })} className="cf-input mt-1 text-sm" aria-describedby="schedule-date-hint" />
          <span id="schedule-date-hint" className="mt-0.5 block text-[10px] font-normal text-[var(--muted-foreground)]">{value.date ? formatLocalDate(value.date, locale) : "Choose a day"}</span>
        </label>
        <fieldset className="text-[11px] font-semibold">
          <legend>Time</legend>
          {/* Explicit selects: native time inputs follow the OS clock, not the chosen locale. */}
          <div className="mt-1 flex gap-1">
            <select aria-label="Hour" required value={hour} onChange={(event) => onChange({ ...value, time: `${event.target.value}:${minute || "00"}` })} className="cf-input text-sm">
              <option value="" disabled>--</option>
              {HOURS.map((item) => <option key={item} value={item}>{hourLabel(item, locale, twentyFour)}</option>)}
            </select>
            <select aria-label="Minute" required value={minute} onChange={(event) => onChange({ ...value, time: `${hour || "00"}:${event.target.value}` })} className="cf-input text-sm">
              <option value="" disabled>--</option>
              {MINUTES.map((item) => <option key={item} value={item}>{item}</option>)}
            </select>
          </div>
          <span className="mt-0.5 block text-[10px] font-normal text-[var(--muted-foreground)]">{twentyFour ? "24-hour clock" : "12-hour clock"}</span>
        </fieldset>
        <label className="text-[11px] font-semibold">Time zone
          <select value={value.timezone} onChange={(event) => onChange({ ...value, timezone: event.target.value })} className="cf-input mt-1 text-sm">
            {zones.map((zone) => <option key={zone} value={zone}>{zone}</option>)}
          </select>
          <span className="mt-0.5 block text-[10px] font-normal text-[var(--muted-foreground)]">{zoneLabel(value.timezone, ok ? shown : null)}</span>
        </label>
      </div>
      {value.date && value.time && (
        <div role="status" className={cn("rounded-xl border px-3 py-2 text-xs", ok ? "border-emerald-200 bg-emerald-50 text-emerald-900" : "border-amber-200 bg-amber-50 text-amber-900")}>
          {ok ? (
            <>
              <p className="flex items-center gap-1.5 font-semibold"><CalendarClock className="size-3.5" /> Scheduled for</p>
              <p className="mt-0.5 text-sm font-semibold">{formatScheduleConfirmation(shown.publish_at!, value.timezone, locale)}</p>
              <p className="mt-0.5">{zoneLabel(value.timezone, shown)}</p>
            </>
          ) : (
            <p>{shown?.message ?? "Checking this time…"}</p>
          )}
        </div>
      )}
    </div>
  );
}
