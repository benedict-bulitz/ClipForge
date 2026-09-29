import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import {
  dayContextLine,
  dayLabel,
  formatNormalized,
  freshnessLabel,
  isManualOverride,
  learningStatusText,
  readRecommendation,
  recommendationLabel,
  scheduleModeLabel,
  slotStatusLabel,
  slotTakenMessage,
  slotsForCount,
  validateSlotDraft,
  zonedToday,
  type ScheduleFreshness,
  type ScheduleLearning,
  type SlotRecommendation,
} from "./youtube-schedule.ts";

const sheet = readFileSync(new URL("../components/youtube-publish-sheet.tsx", import.meta.url), "utf8");
const settingsView = readFileSync(new URL("../components/youtube-publishing-schedule.tsx", import.meta.url), "utf8");
const card = readFileSync(new URL("../components/youtube-connection-card.tsx", import.meta.url), "utf8");
const helpers = readFileSync(new URL("./youtube-schedule.ts", import.meta.url), "utf8");

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
  assert.match(card, /\{connected && <YouTubePublishingSchedule \/>\}/);
});

test("upload sheet pre-selects the next free slot from the planner", () => {
  assert.equal(recommendationLabel(recommendation(), NOW, "de-DE"), "Today · 21:30");
  assert.equal(recommendationLabel(recommendation({ local_date: "2026-09-29", local_time: "17:00" }), NOW, "de-DE"), "Tomorrow · 17:00");
  assert.equal(dayContextLine(recommendation(), NOW, "de-DE"), "Today: 12:30 occupied · 17:00 occupied · 21:30 selected");
  assert.match(sheet, /getPublishingDraft\(project\.id, region, language, detectTimeZone\(\)\)/);
  assert.match(sheet, /schedule: next\.options\.schedule \?\?/);
  assert.match(sheet, /Why: \{shown\.reason\}/);
  assert.match(sheet, /Recommended: /);
});

test("a manual time is respected and never snapped back", () => {
  assert.equal(isManualOverride({ date: "2026-09-28", time: "21:30", timezone: BERLIN }, recommendation()), false);
  assert.equal(isManualOverride({ date: "2026-09-28", time: "20:45", timezone: BERLIN }, recommendation()), true);
  assert.equal(isManualOverride(null, recommendation()), false);
  // Editing the fields marks the upload manual; the planner is only applied on an explicit action.
  assert.match(sheet, /onChange=\{\(schedule\) => update\(\{ schedule, schedule_source: "manual" \}\)\}/);
  assert.match(sheet, /if \(next\.recommendation && options\.schedule_source !== "manual"\) applySlot/);
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
