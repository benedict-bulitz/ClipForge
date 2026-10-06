import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { safeLocale } from "./youtube.ts";
import {
  applyRecommendedSlot,
  dayContextLine,
  editSchedule,
  followRecommendation,
  readConflict,
  scheduleKey,
  dayLabel,
  formatNormalized,
  freshnessLabel,
  isManualOverride,
  learningStatusText,
  readRecommendation,
  recommendationLabel,
  scheduleLoadError,
  scheduleModeLabel,
  slotStatusLabel,
  slotTakenMessage,
  slotsForCount,
  validateSlotDraft,
  zonedToday,
  type ScheduleFreshness,
  type ScheduleSelection,
  type ScheduleLearning,
  type SlotRecommendation,
} from "./youtube-schedule.ts";

const sheet = readFileSync(new URL("../components/youtube-publish-sheet.tsx", import.meta.url), "utf8");
const settingsView = readFileSync(new URL("../components/youtube-publishing-schedule.tsx", import.meta.url), "utf8");
const card = readFileSync(new URL("../components/publishing-integrations.tsx", import.meta.url), "utf8");
const helpers = readFileSync(new URL("./youtube-schedule.ts", import.meta.url), "utf8");
const fields = readFileSync(new URL("../components/youtube-schedule-fields.tsx", import.meta.url), "utf8");

// The backend's seed presets exactly as GET /api/youtube/schedule returns them.
const PRESETS: Record<string, string[]> = {
  "1": ["20:00"],
  "2": ["15:00", "20:30"],
  "3": ["12:30", "17:00", "21:30"],
  "4": ["11:00", "15:00", "18:30", "22:00"],
  "5": ["09:30", "12:30", "16:00", "19:30", "22:30"],
};
const NOW = new Date("2026-09-28T16:00:00Z"); // 18:00 in Berlin
const BERLIN = "Europe/Berlin";

const slot = (local_time: string, status: SlotRecommendation["day"][number]["status"], position: number) => ({
  local_time, status, position, publish_at: null, abbreviation: "CEST", utc_offset: "UTC+02:00", occupant: null, message: null,
});

/** An upload's schedule state before anything was chosen (plus an unrelated field). */
const blank = (): ScheduleSelection & { title: string } => ({ schedule: null, schedule_source: null, title: "t" });

const recommendation = (overrides: Partial<SlotRecommendation> = {}): SlotRecommendation => ({
  choice: { date: "2026-09-28", time: "21:30", timezone: BERLIN },
  publish_at: "2026-09-28T19:30:00Z",
  local_date: "2026-09-28",
  local_time: "21:30",
  timezone: BERLIN,
  abbreviation: "CEST",
  utc_offset: "UTC+02:00",
  day_offset: 0,
  slot_position: 2,
  reason: "Next free slot in your 3-videos/day schedule",
  day: [slot("12:30", "published", 0), slot("17:00", "scheduled", 1), slot("21:30", "free", 2)],
  ...overrides,
});

test("changing videos/day loads that count's starter times into the slot editor", () => {
  for (const count of [1, 2, 3, 4, 5]) {
    assert.deepEqual(slotsForCount(count, PRESETS), PRESETS[String(count)]);
    assert.equal(slotsForCount(count, PRESETS).length, count);
  }
  assert.match(settingsView, /onClick=\{\(\) => set\(\{ videos_per_day: count, slots: slotsForCount\(count, presets\) \}\)\}/);
});

test("seed times load from the backend presets and are labelled as a starter schedule", () => {
  assert.deepEqual(slotsForCount(3, PRESETS), ["12:30", "17:00", "21:30"]);
  assert.equal(scheduleModeLabel("seed"), "Starter schedule");
  assert.equal(scheduleModeLabel("manual"), "Custom schedule");
  assert.match(settingsView, /seed_presets/);
});

test("custom slot edits are validated and saved through the API", () => {
  assert.deepEqual(validateSlotDraft(["12:00", "16:30", "21:00"], 3), { errors: [], warnings: [] });
  assert.ok(validateSlotDraft(["12:00", "16:30"], 3).errors.length);
  assert.ok(validateSlotDraft(["12:00", "12:00", "21:00"], 3).errors.length);
  assert.ok(validateSlotDraft(["12:00", "26:00", "21:00"], 3).errors.length);
  const close = validateSlotDraft(["12:00", "12:30", "21:00"], 3);
  assert.equal(close.errors.length, 0); // not over-policed: only a hint
  assert.equal(close.warnings.length, 1);
  assert.match(settingsView, /saveYouTubeSchedule\(draft\)/);
  assert.match(card, /platform === "youtube" && connected > 0 && \([^]*<YouTubePublishingSchedule \/>/);
});

test("the recommendation prefills date and time as the default choice", () => {
  assert.equal(recommendationLabel(recommendation(), NOW, "de-DE"), "Today · 21:30");
  assert.equal(recommendationLabel(recommendation({ local_date: "2026-09-29", local_time: "17:00" }), NOW, "de-DE"), "Tomorrow · 17:00");
  assert.equal(dayContextLine(recommendation(), NOW, "de-DE"), "Today: 12:30 occupied · 17:00 occupied · 21:30 selected");
  assert.deepEqual(applyRecommendedSlot(blank(), recommendation()), { schedule: { date: "2026-09-28", time: "21:30", timezone: BERLIN }, schedule_source: "auto", title: "t" });
  assert.match(sheet, /getPublishingDraft\(project\.id, region, language, detectTimeZone\(\), accountId\)/);
  assert.match(sheet, /schedule: next\.options\.schedule \?\?/);
  assert.match(sheet, /Reason: \{shown\.reason\}/);
  assert.match(sheet, /"Recommended slot"/);
});

test("date, time and time zone controls are always visible when scheduling", () => {
  // No toggle, no hidden mode: the fields render whenever Visibility = Schedule.
  assert.doesNotMatch(sheet, /showTimeFields|Change date or time|<ScheduleFields[^>]*\bhidden\b/);
  const scheduling = sheet.slice(sheet.indexOf('{options.visibility === "schedule" && options.schedule && ('), sheet.indexOf("</Section>", sheet.indexOf('{options.visibility === "schedule" && options.schedule && (')));
  assert.match(scheduling, /<ScheduleFields\s+value=\{options\.schedule\}/);
  assert.doesNotMatch(scheduling, /Advanced/);
  for (const label of ['aria-label="Hour"', 'aria-label="Minute"', 'type="date"', "Time zone", "zoneLabel(value.timezone"]) {
    assert.ok(fields.includes(label), label);
  }
});

test("changing the date or the time makes it a manual override", () => {
  const auto = applyRecommendedSlot(blank(), recommendation());
  const newDate = editSchedule(auto, { ...auto.schedule!, date: "2026-09-30" });
  assert.deepEqual([newDate.schedule_source, newDate.schedule!.date, newDate.schedule!.time, newDate.title], ["manual", "2026-09-30", "21:30", "t"]);
  const newHour = editSchedule(auto, { ...auto.schedule!, time: "19:30" });
  const newMinute = editSchedule(auto, { ...auto.schedule!, time: "21:45" });
  assert.deepEqual([newHour.schedule_source, newHour.schedule!.time], ["manual", "19:30"]);
  assert.deepEqual([newMinute.schedule_source, newMinute.schedule!.time], ["manual", "21:45"]);
  assert.equal(isManualOverride(newHour.schedule, recommendation()), true);
  assert.match(sheet, /onChange=\{\(schedule\) => setOptions\(\(current\) => \(current \? editSchedule\(current, schedule\) : current\)\)\}/);
});

test("re-renders and schedule refreshes never restore the recommendation over a manual time", () => {
  const manual = editSchedule(applyRecommendedSlot(blank(), recommendation()), { date: "2026-09-30", time: "08:15", timezone: BERLIN });
  const newer = recommendation({ choice: { date: "2026-09-29", time: "12:30", timezone: BERLIN }, local_date: "2026-09-29", local_time: "12:30" });
  assert.deepEqual(followRecommendation(manual, newer), manual); // refresh with a new slot
  assert.deepEqual(followRecommendation(manual, recommendation()), manual); // same slot again
  assert.deepEqual(followRecommendation(manual, null), manual);
  // An automatic selection does follow a refreshed recommendation.
  const auto = applyRecommendedSlot(blank(), recommendation());
  assert.deepEqual(followRecommendation(auto, newer).schedule, newer.choice);
  // The sheet only ever applies a refresh through followRecommendation, and the draft is loaded once.
  assert.match(sheet, /setOptions\(\(current\) => \(current \? followRecommendation\(current, next\.recommendation\) : current\)\)/);
  // ...once per selected YouTube channel (the unified sheet re-keys it per account).
  assert.match(sheet, /\}, \[project\.id, region, language, accountId\]\);/);
});

test('"Use recommended slot" restores the recommendation explicitly', () => {
  const manual = editSchedule(applyRecommendedSlot(blank(), recommendation()), { date: "2026-09-30", time: "08:15", timezone: BERLIN });
  const restored = applyRecommendedSlot(manual, recommendation());
  assert.deepEqual(restored, { schedule: recommendation().choice, schedule_source: "auto", title: "t" });
  assert.match(sheet, /Use recommended slot/);
  assert.match(sheet, /onClick=\{\(\) => onApply\(shown, !recommended\)\}/);
});

test("the manual choice is sent unchanged through preflight and upload", () => {
  // The preflight payload is the options as they are; nothing rewrites the schedule on the way.
  assert.match(sheet, /const payload = \{ \.\.\.options, schedule: options\.visibility === "schedule" \? options\.schedule : null \};/);
  assert.match(sheet, /schedule: wantsSchedule \? options\.schedule : null, schedule_source: wantsSchedule \? options\.schedule_source \?\? "manual" : null/);
});

test("a conflicting manual time is warned with Keep anyway / Use next recommended slot", () => {
  const conflict = readConflict({ conflict: { reason: "occupied", message: "Selected time conflicts with another scheduled video.", occupant: null, recommendation: recommendation() } });
  assert.equal(conflict?.message, "Selected time conflicts with another scheduled video.");
  assert.equal(readConflict({}), null);
  // "Keep anyway" applies only to the exact time it was given for.
  assert.equal(scheduleKey({ date: "2026-09-30", time: "08:15", timezone: BERLIN }), "2026-09-30T08:15@Europe/Berlin");
  assert.notEqual(scheduleKey({ date: "2026-09-30", time: "08:20", timezone: BERLIN }), scheduleKey({ date: "2026-09-30", time: "08:15", timezone: BERLIN }));
  for (const text of ["Keep anyway", "Use next recommended slot", 'reason.code === "schedule_conflict"', "result.schedule_conflict"]) {
    assert.ok(sheet.includes(text), text);
  }
  assert.match(sheet, /keptConflictFor === scheduleKey\(options\.schedule\)/);
  assert.match(sheet, /auto && useCachedSchedule, keepConflict/);
  // Never silently replaced: only the explicit button applies the next slot.
  assert.match(sheet, /onUseNext=\{\(slot\) => \{ setConflict\(null\); applySlot\(slot\); \}\}/);
});

test("a manual time inside the lead time is an inline error, never moved", () => {
  // The backend's "past" resolution is shown inline next to the fields and blocks the upload.
  assert.ok(fields.includes("shown?.message"));
  assert.match(sheet, /const blocked = issues\.length > 0/);
  assert.doesNotMatch(sheet, /MIN_SCHEDULE_LEAD|addMinutes/);
});

test("a manual override never edits the channel's Smart Schedule", () => {
  assert.doesNotMatch(sheet, /saveYouTubeSchedule|applyLearnedYouTubeSchedule/);
  assert.match(sheet, /your Smart Schedule is unchanged/);
});

test("an occupied slot causes a recalculation with the next free slot", () => {
  assert.equal(slotTakenMessage("That slot was just taken.", recommendation(), NOW, "de-DE"), "That slot was just taken. Next available slot: Today · 21:30");
  assert.match(slotTakenMessage("That slot was just taken.", null, NOW, "de-DE"), /choose a time manually/);
  assert.deepEqual(readRecommendation({ recommendation: recommendation() })?.choice, recommendation().choice);
  assert.equal(readRecommendation({ recommendation: null }), null);
  assert.match(sheet, /reason\.code === "slot_taken"/);
  assert.match(sheet, /applySlot\(next, useCachedSchedule\)/);
});

test("a stale or unverifiable schedule is labelled, never hidden", () => {
  const fresh: ScheduleFreshness = { checked_at: "2026-09-28T15:58:00Z", age_seconds: 120, fresh: true, usable: true, complete: true, verified: true, error: null };
  assert.equal(freshnessLabel(fresh), "Checked with YouTube 2 min ago");
  const stale = { ...fresh, age_seconds: 8 * 60, fresh: false, verified: false, error: { code: "quota_exceeded", message: "Quota exceeded." } };
  assert.equal(freshnessLabel(stale, true), "Based on schedule checked 8 min ago");
  assert.equal(freshnessLabel(undefined), "YouTube schedule not checked yet");
  for (const text of ["Could not verify YouTube schedule.", "Use cached schedule", "Choose time manually", "Retry"]) {
    assert.ok(sheet.includes(text), text);
  }
  assert.match(sheet, /auto && useCachedSchedule/); // cached data is only used when the user chose it
});

test("learned mode is unavailable below the sample threshold", () => {
  const learning: ScheduleLearning = {
    available: false, eligible_count: 7, min_eligible: 30, min_window_samples: 5, reference_age_bucket: "24h", metric: "engagedViews",
    current: ["12:30", "17:00", "21:30"], suggested: null, windows: [], reason: null, caveat: "", auto_applied: false,
  };
  assert.equal(learningStatusText(learning), "Learned schedule becomes available after 30 published Shorts with analytics (7 so far).");
  assert.match(settingsView, /learning\.available && learning\.suggested &&/);
});

test("a learned recommendation shows its sample size and needs approval", () => {
  const learning: ScheduleLearning = {
    available: true, eligible_count: 47, based_on: 47, min_eligible: 30, min_window_samples: 5, reference_age_bucket: "24h", metric: "engagedViews",
    current: ["12:30", "17:00", "21:30"], suggested: ["13:00", "18:00", "21:30"], differs: true, windows: [], reason: null, caveat: "", auto_applied: false,
  };
  assert.equal(learningStatusText(learning), "Suggested from channel data · based on 47 eligible Shorts");
  assert.equal(scheduleModeLabel("learned", 47), "Learned from your channel · 47 Shorts");
  assert.equal(formatNormalized(1.0842), "1.08×");
  assert.match(settingsView, /Apply learned schedule/);
  assert.match(settingsView, /Based on: \{learning\.based_on\} eligible Shorts/);
});

test("no 'best time' or 'optimized' wording for starter times", () => {
  for (const source of [sheet, settingsView, helpers]) {
    assert.doesNotMatch(source, /best time|optimi[sz]ed|optimal/i);
  }
  for (const status of ["free", "published", "scheduled", "reserved", "missed", "not_needed", "unavailable"] as const) {
    assert.doesNotMatch(slotStatusLabel(status), /best|optim/i);
  }
});

test("calendar days are computed in the schedule's zone", () => {
  const lateUtc = new Date("2026-09-28T22:30:00Z"); // already 29 Sept in Berlin
  assert.equal(zonedToday(BERLIN, lateUtc), "2026-09-29");
  assert.equal(zonedToday("America/New_York", lateUtc), "2026-09-28");
  assert.equal(dayLabel("2026-09-29", BERLIN, lateUtc, "en-GB"), "Today");
  assert.equal(dayLabel("2026-09-30", BERLIN, lateUtc, "en-GB"), "Tomorrow");
  assert.equal(dayLabel("2026-10-02", BERLIN, lateUtc, "en-GB"), "Fri, 02/10/2026");
});

test("a genuine load failure shows a retryable error; first use is not an error", () => {
  assert.equal(scheduleLoadError(null), "Could not load publishing schedule.");
  assert.equal(scheduleLoadError("Could not load publishing schedule."), "Could not load publishing schedule.");
  assert.equal(scheduleLoadError("Connect a YouTube channel first."), "Could not load publishing schedule. Connect a YouTube channel first.");
  assert.match(settingsView, /setAttempt\(\(value\) => value \+ 1\)/); // Retry re-requests
  assert.match(settingsView, /\[attempt\]\);/);
  assert.match(settingsView, /> Retry<\/Button>/);
  assert.doesNotMatch(settingsView, /The publishing schedule could not be loaded/);
});

test("an invalid browser locale tag never crashes the schedule (real: en-US@posix)", () => {
  assert.equal(safeLocale("en-US@posix"), "en-US");
  assert.equal(safeLocale("de_DE.UTF-8"), "de-DE");
  assert.equal(safeLocale("de-DE"), "de-DE");
  assert.equal(safeLocale(""), "en-US");
  assert.equal(safeLocale("!!"), "en-US");
  assert.equal(dayLabel("2026-10-02", BERLIN, NOW, safeLocale("en-US@posix")), "Fri, 10/02/2026");
});
