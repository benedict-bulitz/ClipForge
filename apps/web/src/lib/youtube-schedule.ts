/** Smart Slot Planner: API shapes and pure presentation helpers. */

import type { ScheduleChoice } from "./youtube.ts";

export type SlotStatus = "free" | "published" | "scheduled" | "reserved" | "missed" | "not_needed" | "unavailable";

export type SlotView = {
  local_time: string;
  position: number;
  status: SlotStatus;
  publish_at: string | null;
  abbreviation: string | null;
  utc_offset: string | null;
  occupant: { kind: string; at: string; source: string } | null;
  message: string | null;
};

export type DayView = {
  date: string;
  target: number;
  count: number;
  published: number;
  scheduled: number;
  reserved: number;
  full: boolean;
  slots: SlotView[];
  other: Array<{ kind: string; at: string; source: string }>;
};

export type SlotRecommendation = {
  choice: ScheduleChoice;
  publish_at: string;
  local_date: string;
  local_time: string;
  timezone: string;
  abbreviation: string | null;
  utc_offset: string | null;
  day_offset: number;
  slot_position: number;
  reason: string;
  day: SlotView[];
};

export type ScheduleFreshness = {
  checked_at: string | null;
  age_seconds: number | null;
  fresh: boolean;
  usable: boolean;
  complete: boolean;
  verified: boolean;
  error: { code: string; message: string } | null;
};

export type ScheduleMode = "seed" | "manual" | "learned";

export type PublishingSchedule = {
  channel_id: string;
  timezone: string;
  videos_per_day: number;
  mode: ScheduleMode;
  enabled: boolean;
  slots: string[];
  weekday_slots: Record<string, string[]>;
  occupancy_tolerance_minutes: number;
  min_lead_minutes: number;
  horizon_days: number;
  learned_sample_size: number | null;
  learned_applied_at: string | null;
  seed_presets: Record<string, string[]>;
};

export type SmartStatus = "verified" | "stale_cache" | "unverified" | "disabled" | "not_connected";

export type SmartScheduleState = {
  status: SmartStatus;
  enabled: boolean;
  schedule?: PublishingSchedule;
  freshness?: ScheduleFreshness;
  recommendation?: SlotRecommendation | null;
  cached_recommendation?: SlotRecommendation | null;
  horizon_full?: boolean;
  days?: DayView[];
};

export type LearningWindow = {
  window: string;
  start: string;
  n: number;
  sufficient: boolean;
  median_normalized: number | null;
  p25_normalized: number | null;
  p75_normalized: number | null;
  median_average_view_percentage: number | null;
  median_opening_retention_3s: number | null;
};

export type ScheduleLearning = {
  available: boolean;
  eligible_count: number;
  min_eligible: number;
  min_window_samples: number;
  reference_age_bucket: string;
  metric: string;
  current: string[];
  suggested: string[] | null;
  based_on?: number;
  differs?: boolean;
  windows: LearningWindow[];
  reason?: string | null;
  caveat: string;
  auto_applied: false;
};

export type ScheduleOverview = SmartScheduleState & { learning: ScheduleLearning };

export type ScheduleUpdate = {
  videos_per_day: number;
  timezone: string;
  slots: string[];
  enabled: boolean;
  occupancy_tolerance_minutes: number;
  min_lead_minutes: number;
};

export const VIDEOS_PER_DAY_OPTIONS = [1, 2, 3, 4, 5] as const;

const TIME = /^([01]\d|2[0-3]):[0-5]\d$/;

/** The slot editor after "videos per day" changes: that count's starter times. */
export function slotsForCount(count: number, presets: Record<string, string[]>): string[] {
  return [...(presets[String(count)] ?? [])];
}

/** Mirrors the backend's checks so problems show while typing; the backend decides. */
export function validateSlotDraft(slots: string[], count: number): { errors: string[]; warnings: string[] } {
  const errors: string[] = [];
  const warnings: string[] = [];
  if (slots.length !== count) errors.push(`Set exactly ${count} time${count === 1 ? "" : "s"}.`);
  if (slots.some((slot) => !TIME.test(slot))) errors.push("Use valid times like 17:00.");
  const valid = slots.filter((slot) => TIME.test(slot));
  if (new Set(valid).size !== valid.length) errors.push("Two slots have the same time.");
  const minutes = Array.from(new Set(valid)).map((slot) => Number(slot.slice(0, 2)) * 60 + Number(slot.slice(3))).sort((a, b) => a - b);
  if (minutes.some((value, index) => index > 0 && value - minutes[index - 1] < 60)) {
    warnings.push("Some slots are less than an hour apart.");
  }
  return { errors, warnings };
}

/** Starter times are labelled as defaults; only user-approved learned slots mention channel data. */
export function scheduleModeLabel(mode: ScheduleMode, learnedSampleSize?: number | null): string {
  if (mode === "learned") return learnedSampleSize ? `Learned from your channel · ${learnedSampleSize} Shorts` : "Learned from your channel";
  return mode === "manual" ? "Custom schedule" : "Starter schedule";
}

export function scheduleModeHint(mode: ScheduleMode): string {
  switch (mode) {
    case "seed": return "Default times to start with — not derived from your channel's data. Edit any slot.";
    case "manual": return "Your own times.";
    default: return "Applied by you from your channel's own results. Associations, not guarantees.";
  }
}

export function slotStatusLabel(status: SlotStatus): string {
  return {
    free: "Free",
    published: "✓ Published",
    scheduled: "✓ Scheduled",
    reserved: "Reserved · uploading",
    missed: "Passed",
    not_needed: "Not needed · day full",
    unavailable: "Skipped · clock change",
  }[status];
}

export function isOccupied(status: SlotStatus): boolean {
  return status === "published" || status === "scheduled" || status === "reserved";
}

/** Today's date (YYYY-MM-DD) in the schedule's zone. */
export function zonedToday(timezone: string, now: Date): string {
  const parts = new Intl.DateTimeFormat("en-CA", { timeZone: timezone, year: "numeric", month: "2-digit", day: "2-digit" }).formatToParts(now);
  const get = (type: string) => parts.find((part) => part.type === type)?.value ?? "";
  return `${get("year")}-${get("month")}-${get("day")}`;
}

function addDays(date: string, days: number): string {
  const [year, month, day] = date.split("-").map(Number);
  return new Date(Date.UTC(year, month - 1, day + days)).toISOString().slice(0, 10);
}

/** "Today" | "Tomorrow" | "Wed, 30.09.2026" in the user's locale. */
export function dayLabel(date: string, timezone: string, now: Date, locale: string): string {
  const today = zonedToday(timezone, now);
  if (date === today) return "Today";
  if (date === addDays(today, 1)) return "Tomorrow";
  const [year, month, day] = date.split("-").map(Number);
  return new Intl.DateTimeFormat(locale, { weekday: "short", day: "2-digit", month: "2-digit", year: "numeric", timeZone: "UTC" }).format(new Date(Date.UTC(year, month - 1, day)));
}

/** "Tomorrow · 17:00" - the actual time is always shown. */
export function recommendationLabel(recommendation: Pick<SlotRecommendation, "local_date" | "local_time" | "timezone">, now: Date, locale: string): string {
  return `${dayLabel(recommendation.local_date, recommendation.timezone, now, locale)} · ${recommendation.local_time}`;
}

/** "Europe/Berlin · CEST" */
export function zoneLine(timezone: string, abbreviation: string | null | undefined): string {
  return abbreviation ? `${timezone} · ${abbreviation}` : timezone;
}

/** "Today: 12:30 occupied · 17:00 occupied · 21:30 selected" */
export function dayContextLine(recommendation: SlotRecommendation, now: Date, locale: string): string {
  const parts = recommendation.day.map((slot) => {
    if (slot.position === recommendation.slot_position && slot.local_time === recommendation.local_time) return `${slot.local_time} selected`;
    if (isOccupied(slot.status)) return `${slot.local_time} occupied`;
    if (slot.status === "missed") return `${slot.local_time} passed`;
    return `${slot.local_time} ${slot.status === "free" ? "free" : "skipped"}`;
  });
  return `${dayLabel(recommendation.local_date, recommendation.timezone, now, locale)}: ${parts.join(" · ")}`;
}

/** Staleness is never hidden: "Checked with YouTube 2 min ago" / "Based on schedule checked 8 min ago". */
export function freshnessLabel(freshness: ScheduleFreshness | undefined, fromCache = false): string {
  if (!freshness?.checked_at || freshness.age_seconds === null) return "YouTube schedule not checked yet";
  const minutes = Math.floor(freshness.age_seconds / 60);
  const age = minutes < 1 ? "just now" : minutes < 60 ? `${minutes} min ago` : minutes < 48 * 60 ? `${Math.floor(minutes / 60)} h ago` : `${Math.floor(minutes / 1440)} days ago`;
  return fromCache || !freshness.fresh ? `Based on schedule checked ${age}` : `Checked with YouTube ${age}`;
}

/** The time the user picked is theirs once it differs from the recommendation. */
export function isManualOverride(schedule: ScheduleChoice | null, recommendation: SlotRecommendation | null | undefined): boolean {
  if (!schedule || !recommendation) return false;
  const choice = recommendation.choice;
  return schedule.date !== choice.date || schedule.time !== choice.time || schedule.timezone !== choice.timezone;
}

/** "That slot was just taken. Next available slot: Today · 21:30" */
export function slotTakenMessage(message: string, next: SlotRecommendation | null | undefined, now: Date, locale: string): string {
  return next ? `${message} Next available slot: ${recommendationLabel(next, now, locale)}` : `${message} No free slot in your schedule horizon — choose a time manually.`;
}

export function readRecommendation(detail: Record<string, unknown> | undefined, key = "recommendation"): SlotRecommendation | null {
  const value = detail?.[key];
  return value && typeof value === "object" && "choice" in value ? (value as SlotRecommendation) : null;
}

/** "Could not load publishing schedule." plus the server's reason when it gave one. */
export function scheduleLoadError(serverMessage: string | null | undefined): string {
  const base = "Could not load publishing schedule.";
  const detail = (serverMessage ?? "").trim();
  return detail && detail !== base ? `${base} ${detail}` : base;
}

export function learningStatusText(learning: ScheduleLearning): string {
  if (learning.available) return `Suggested from channel data · based on ${learning.based_on ?? learning.eligible_count} eligible Shorts`;
  return learning.reason ?? `Learned schedule becomes available after ${learning.min_eligible} published Shorts with analytics (${learning.eligible_count} so far).`;
}

/** "1.08×" - relative to the channel median at the same age. */
export function formatNormalized(value: number | null | undefined): string {
  return value === null || value === undefined ? "–" : `${value.toFixed(2)}×`;
}

export function timeOptions(stepMinutes = 5): string[] {
  const values: string[] = [];
  for (let minute = 0; minute < 24 * 60; minute += stepMinutes) {
    values.push(`${String(Math.floor(minute / 60)).padStart(2, "0")}:${String(minute % 60).padStart(2, "0")}`);
  }
  return values;
}
