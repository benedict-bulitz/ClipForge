/**
 * Channel Performance overview: shape of /api/videos/performance and pure
 * presentation helpers. All numbers come from stored YouTube data: the
 * Analytics API per video, or - until YouTube has processed a video's
 * analytics - the views/likes/comments its Data API already reports (the
 * same numbers as the video list). A missing value is "—", never a zero.
 */

export type PerformanceScope = "last10" | "28d" | "90d" | "all";

export type MetricKey =
  | "avg_views" | "avg_view_duration" | "avg_view_percentage" | "engaged_view_rate" | "stayed_to_watch" | "avg_video_length"
  | "avg_likes" | "avg_comments" | "avg_shares" | "avg_subscribers_gained" | "avg_subscribers_lost" | "avg_net_subscribers"
  | "avg_watch_time_minutes" | "likes_per_1k_views" | "comments_per_1k_views" | "shares_per_1k_views" | "subscribers_per_1k_views";

export type MetricResult = { value: number | null; n: number; missing: number; method?: string | null; source?: string; available?: boolean };
export type Trend = { change: number; direction: "up" | "down" | "flat"; n_current: number; n_previous: number; age_hours: number };
export type DiagnosisStatus = "weaker" | "normal" | "stronger" | "insufficient_data";
export type DiagnosisEntry = { status: DiagnosisStatus; evidence: string; value: number | null; n: number; baseline_n: number; baseline_median?: number };
export type DiagnosisKey = "hook" | "retention" | "engagement" | "conversion";

export type PerformanceOverview = {
  scope: PerformanceScope;
  scopes: PerformanceScope[];
  channel: { id: string | null; title: string | null };
  video_count: number;
  eligible_total: number;
  previous_count: number;
  updated_at: string | null;
  value_basis: string;
  /** Videos per source: Analytics API snapshot, or videos.list statistics only. */
  sources?: { youtube_analytics_api: number; youtube_data_api_videos_list: number };
  primary: MetricKey[];
  secondary: MetricKey[];
  metrics: Record<MetricKey, MetricResult>;
  trends: Partial<Record<MetricKey, Trend>>;
  trend_age_hours: number | null;
  swipe_away: { available: false; reason: string };
  diagnosis: Partial<Record<DiagnosisKey, DiagnosisEntry>>;
  diagnosis_baseline: { videos: number; min_sample: number };
  main_signal: { code: string; message: string } | null;
};

export const SCOPE_OPTIONS: Array<[PerformanceScope, string]> = [
  ["last10", "Letzte 10 Videos"],
  ["28d", "28 Tage"],
  ["90d", "90 Tage"],
  ["all", "Alle"],
];

export const METRIC_LABELS: Record<MetricKey, string> = {
  avg_views: "Avg Views",
  avg_view_duration: "Avg View Duration",
  avg_view_percentage: "Avg View %",
  engaged_view_rate: "Engaged View Rate",
  stayed_to_watch: "Stayed to watch",
  avg_video_length: "Avg Video Length",
  avg_likes: "Avg Likes",
  avg_comments: "Avg Comments",
  avg_shares: "Avg Shares",
  avg_subscribers_gained: "Avg Subs gained",
  avg_subscribers_lost: "Avg Subs lost",
  avg_net_subscribers: "Avg Net Subs",
  avg_watch_time_minutes: "Avg Watch Time",
  likes_per_1k_views: "Likes / 1k Views",
  comments_per_1k_views: "Comments / 1k Views",
  shares_per_1k_views: "Shares / 1k Views",
  subscribers_per_1k_views: "Subs / 1k Views",
};

export const METRIC_TOOLTIPS: Partial<Record<MetricKey, string>> = {
  avg_views: "Durchschnittliche Aufrufe je Video (YouTube Analytics, letzter Abruf je Video; ohne Analytics noch die Aufrufe aus der Videoliste).",
  avg_view_duration: "Gesamte Wiedergabezeit ÷ Gesamtaufrufe (nach Aufrufen gewichtet).",
  avg_view_percentage: "Nach Wiedergabezeit gewichteter Durchschnitt – nicht der einfache Mittelwert der Videos.",
  engaged_view_rate: "engagedViews ÷ views (YouTube Analytics). Das ist KEINE Swipe-away-Rate – die gibt es in der API nicht.",
  stayed_to_watch: "Aus YouTube Studio importiert; nicht über die API verfügbar.",
  avg_video_length: "Durchschnittliche Länge der gerenderten Videos.",
  avg_watch_time_minutes: "Durchschnittliche Wiedergabezeit je Video (estimatedMinutesWatched).",
  likes_per_1k_views: "Likes je 1.000 Aufrufe – vergleichbar trotz unterschiedlicher Reichweite.",
  comments_per_1k_views: "Kommentare je 1.000 Aufrufe.",
  shares_per_1k_views: "Shares je 1.000 Aufrufe.",
  subscribers_per_1k_views: "Gewonnene Abos je 1.000 Aufrufe.",
};

export const DIAGNOSIS_LABELS: Record<DiagnosisKey, string> = { hook: "Hook", retention: "Retention", engagement: "Engagement", conversion: "Conversion" };

export const EVIDENCE_LABELS: Record<string, string> = {
  engaged_view_rate: "Signal: Engaged View Rate",
  stayed_to_watch: "Signal: Stayed to watch (Studio)",
  avg_view_percentage: "Signal: Avg View %",
  interactions_per_1k_views: "Signal: Likes + Kommentare + Shares je 1k Aufrufe",
  subscribers_per_1k_views: "Signal: Abos je 1k Aufrufe",
};

/** The 4th primary slot: real Stayed-to-watch when imported, otherwise the Engaged View Rate. */
export function primaryMetrics(overview: Pick<PerformanceOverview, "primary" | "metrics">): MetricKey[] {
  if (!overview.metrics.stayed_to_watch?.available) return overview.primary;
  return overview.primary.map((key) => (key === "engaged_view_rate" ? "stayed_to_watch" : key));
}

/** Secondary list; the Engaged View Rate moves here when Stayed-to-watch takes its slot. */
export function secondaryMetrics(overview: Pick<PerformanceOverview, "primary" | "secondary" | "metrics">): MetricKey[] {
  return overview.metrics.stayed_to_watch?.available ? ["engaged_view_rate", ...overview.secondary] : overview.secondary;
}

function clock(seconds: number): string {
  const safe = Math.max(0, Math.round(seconds));
  return `${Math.floor(safe / 60)}:${String(safe % 60).padStart(2, "0")}`;
}

/** "—" whenever the value is unavailable. */
export function formatPerformanceValue(key: MetricKey, value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  switch (key) {
    case "avg_view_duration":
      return value < 60 ? `${value.toFixed(1)} s` : clock(value);
    case "avg_video_length":
      return clock(value);
    case "avg_view_percentage":
    case "stayed_to_watch":
      return `${value.toFixed(1)}%`;
    case "engaged_view_rate":
      return `${(value * 100).toFixed(1)}%`;
    case "avg_watch_time_minutes":
      return value < 1 ? `${Math.round(value * 60)} s` : `${value.toFixed(1)} min`;
    case "likes_per_1k_views":
    case "comments_per_1k_views":
    case "shares_per_1k_views":
    case "subscribers_per_1k_views":
      return value.toFixed(1);
    case "avg_net_subscribers":
      return `${value > 0 ? "+" : ""}${value.toFixed(value !== 0 && Math.abs(value) < 10 ? 1 : 0)}`;
    default:
      return Math.abs(value) < 10 && !Number.isInteger(value) ? value.toFixed(1) : Math.round(value).toLocaleString("en-US"); // same grouping as the video list below
  }
}

/** "↑ 12%" / "↓ 8%" / "≈"; nothing without a comparable preceding cohort. */
export function trendLabel(trend: Trend | undefined): string | null {
  if (!trend) return null;
  if (trend.direction === "flat") return "≈";
  const pct = Math.round(Math.abs(trend.change) * 100);
  return `${trend.direction === "up" ? "↑" : "↓"} ${pct}%`;
}

export function trendTitle(trend: Trend | undefined): string | undefined {
  if (!trend) return undefined;
  return `Vergleich mit den vorherigen ${trend.n_previous} Videos bei gleichem Videoalter (~${Math.round(trend.age_hours)} h)`;
}

export function diagnosisLabel(status: DiagnosisStatus | undefined): string {
  switch (status) {
    case "weaker": return "↓ weaker";
    case "stronger": return "↑ strong";
    case "normal": return "→ normal";
    default: return "Noch nicht genug Daten";
  }
}

/** "10 Videos · 3 noch ohne Analytics · zuletzt aktualisiert 30.09.2026, 14:05" */
export function cohortLine(overview: Pick<PerformanceOverview, "video_count" | "updated_at" | "sources">, format: (iso: string) => string): string {
  const parts = [`${overview.video_count} ${overview.video_count === 1 ? "Video" : "Videos"}`];
  const pending = overview.sources?.youtube_data_api_videos_list ?? 0;
  if (pending > 0) parts.push(`${pending} noch ohne Analytics (nur Aufrufe/Likes/Kommentare)`);
  if (overview.updated_at) parts.push(`zuletzt aktualisiert ${format(overview.updated_at)}`);
  return parts.join(" · ");
}
