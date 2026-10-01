import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import {
  METRIC_LABELS,
  METRIC_TOOLTIPS,
  SCOPE_OPTIONS,
  cohortLine,
  diagnosisLabel,
  formatPerformanceValue,
  primaryMetrics,
  secondaryMetrics,
  trendLabel,
  trendTitle,
  type MetricKey,
  type MetricResult,
  type PerformanceOverview,
} from "./performance.ts";

const component = readFileSync(new URL("../components/channel-performance.tsx", import.meta.url), "utf8");
const library = readFileSync(new URL("../components/video-library.tsx", import.meta.url), "utf8");
const api = readFileSync(new URL("./api.ts", import.meta.url), "utf8");

const PRIMARY: MetricKey[] = ["avg_views", "avg_view_duration", "avg_view_percentage", "engaged_view_rate", "avg_video_length"];
const SECONDARY: MetricKey[] = ["avg_likes", "avg_comments", "avg_shares", "avg_subscribers_gained", "avg_subscribers_lost", "avg_net_subscribers", "avg_watch_time_minutes", "likes_per_1k_views", "comments_per_1k_views", "shares_per_1k_views", "subscribers_per_1k_views"];
const metric = (value: number | null, extra: Partial<MetricResult> = {}): MetricResult => ({ value, n: value === null ? 0 : 3, missing: 0, ...extra });
const overview = (stayed: MetricResult = metric(null, { available: false })) => ({
  primary: PRIMARY, secondary: SECONDARY,
  metrics: { stayed_to_watch: stayed } as PerformanceOverview["metrics"],
});

test("the primary strip is the five diagnostic metrics; the 4th slot is real Stayed-to-watch only when imported", () => {
  assert.deepEqual(primaryMetrics(overview()), PRIMARY);
  assert.deepEqual(secondaryMetrics(overview()), SECONDARY);
  const withStayed = overview(metric(72, { available: true }));
  assert.deepEqual(primaryMetrics(withStayed), ["avg_views", "avg_view_duration", "avg_view_percentage", "stayed_to_watch", "avg_video_length"]);
  assert.equal(secondaryMetrics(withStayed)[0], "engaged_view_rate");
});

test("unavailable values are a dash, a real zero stays zero", () => {
  assert.equal(formatPerformanceValue("avg_views", null), "—");
  assert.equal(formatPerformanceValue("avg_view_percentage", undefined), "—");
  assert.equal(formatPerformanceValue("avg_views", 0), "0");
  assert.equal(formatPerformanceValue("avg_views", 12_345.4), "12,345"); // same grouping as the list below
  assert.equal(formatPerformanceValue("avg_view_duration", 14.26), "14.3 s");
  assert.equal(formatPerformanceValue("avg_view_duration", 75), "1:15");
  assert.equal(formatPerformanceValue("avg_view_percentage", 64.25), "64.3%");
  assert.equal(formatPerformanceValue("engaged_view_rate", 0.612), "61.2%");
  assert.equal(formatPerformanceValue("avg_video_length", 44.6), "0:45");
  assert.equal(formatPerformanceValue("likes_per_1k_views", 14), "14.0");
  assert.equal(formatPerformanceValue("avg_net_subscribers", 2.5), "+2.5");
  assert.equal(formatPerformanceValue("avg_net_subscribers", -3), "-3.0");
  assert.equal(formatPerformanceValue("avg_watch_time_minutes", 0.5), "30 s");
});

test("engagedViews/views is labelled as Engaged View Rate and never as swipe-away", () => {
  assert.equal(METRIC_LABELS.engaged_view_rate, "Engaged View Rate");
  assert.ok(!Object.values(METRIC_LABELS).some((label) => /swipe/i.test(label)));
  assert.match(METRIC_TOOLTIPS.engaged_view_rate ?? "", /KEINE Swipe-away-Rate/);
  assert.match(component, /Swipe-away: nicht verfügbar/);
  assert.doesNotMatch(component, /Swipe-away-Rate|swipe_away_rate/);
});

test("trends are small and only shown when the backend found a comparable cohort", () => {
  const base = { n_current: 10, n_previous: 10, age_hours: 72 };
  assert.equal(trendLabel({ ...base, change: 0.12, direction: "up" }), "↑ 12%");
  assert.equal(trendLabel({ ...base, change: -0.08, direction: "down" }), "↓ 8%");
  assert.equal(trendLabel({ ...base, change: 0.01, direction: "flat" }), "≈");
  assert.equal(trendLabel(undefined), null);
  assert.match(trendTitle({ ...base, change: 0.12, direction: "up" }) ?? "", /vorherigen 10 Videos bei gleichem Videoalter \(~72 h\)/);
  assert.match(component, /mono shrink-0 text-\[10px\] text-\[var\(--muted-foreground\)\]/); // not colour-coded
});

test("diagnosis wording and the insufficient-data state", () => {
  assert.equal(diagnosisLabel("weaker"), "↓ weaker");
  assert.equal(diagnosisLabel("normal"), "→ normal");
  assert.equal(diagnosisLabel("stronger"), "↑ strong");
  assert.equal(diagnosisLabel("insufficient_data"), "Noch nicht genug Daten");
  assert.equal(diagnosisLabel(undefined), "Noch nicht genug Daten");
  for (const key of ["Hook", "Retention", "Engagement", "Conversion"]) assert.match(readFileSync(new URL("./performance.ts", import.meta.url), "utf8"), new RegExp(`"${key}"`));
  // at most one main signal line
  assert.equal((component.match(/Auffällig:/g) ?? []).length, 1);
  assert.match(component, /\{overview\.main_signal && \(/);
});

test("scope control: four compact options applied to the one overview request", () => {
  assert.deepEqual(SCOPE_OPTIONS.map(([key]) => key), ["last10", "28d", "90d", "all"]);
  assert.deepEqual(SCOPE_OPTIONS.map(([, label]) => label), ["Letzte 10 Videos", "28 Tage", "90 Tage", "Alle"]);
  assert.match(api, /`\/videos\/performance\?scope=\$\{encodeURIComponent\(scope\)\}`/);
  assert.equal((component.match(/getPerformanceOverview\(/g) ?? []).length, 1); // one request, not one per metric
  assert.match(component, /\}, \[scope, refreshKey\]\);/);
  assert.equal(cohortLine({ video_count: 10, updated_at: "2026-09-30T12:00:00Z" }, () => "30.09.2026"), "10 Videos · zuletzt aktualisiert 30.09.2026");
  assert.equal(cohortLine({ video_count: 1, updated_at: null }, () => ""), "1 Video");
});

test("a cohort whose analytics YouTube has not processed yet still shows real numbers, labelled", () => {
  const sources = { youtube_analytics_api: 1, youtube_data_api_videos_list: 2 };
  assert.equal(
    cohortLine({ video_count: 3, updated_at: "2026-09-30T12:00:00Z", sources }, () => "30.09.2026"),
    "3 Videos · 2 noch ohne Analytics (nur Aufrufe/Likes/Kommentare) · zuletzt aktualisiert 30.09.2026",
  );
  assert.equal(cohortLine({ video_count: 2, updated_at: null, sources: { youtube_analytics_api: 2, youtube_data_api_videos_list: 0 } }, () => ""), "2 Videos");
  // Views without retention: the panel shows the views and "—" for the rest, not "no data".
  assert.equal(formatPerformanceValue("avg_views", 956), "956");
  assert.equal(formatPerformanceValue("avg_view_duration", null), "—");
  assert.equal(formatPerformanceValue("avg_view_percentage", null), "—");
  assert.equal(formatPerformanceValue("engaged_view_rate", null), "—");
  assert.notEqual(formatPerformanceValue("avg_views", 0), "—"); // a real 0 stays a 0
  assert.match(component, /const empty = overview !== null && overview\.video_count === 0;/);
});

test("secondary metrics are collapsed by default", () => {
  assert.match(component, /const \[expanded, setExpanded\] = useState\(false\);/);
  assert.match(component, /\{expanded && \(\n\s+<dl id="channel-performance-more"/);
  assert.match(component, /\{expanded \? "Weniger anzeigen" : "Mehr anzeigen"\}/);
  assert.match(component, /aria-expanded=\{expanded\}/);
});

test("compact, overflow-safe layout that reuses the page's card style", () => {
  assert.match(component, /<section className="workspace-card mt-5 p-4"/);
  assert.match(component, /grid grid-cols-2 gap-x-4 gap-y-3 sm:grid-cols-3 lg:grid-cols-5/);
  assert.match(component, /<div className="min-w-0" data-metric=\{name\}/); // grid cells can shrink
  assert.match(component, /truncate text-\[10px\] font-bold uppercase/); // long labels never widen the grid
  assert.match(component, /font-semibold tabular-nums/); // aligned numerals
  assert.doesNotMatch(component, /text-3xl|text-4xl|min-w-\[/); // no oversized dashboard tiles
  assert.match(library, /<ChannelPerformance refreshKey=\{performanceKey\} \/>/);
  assert.match(library, /setPerformanceKey\(\(key\) => key \+ 1\);/); // follows "Refresh recent videos"
});
