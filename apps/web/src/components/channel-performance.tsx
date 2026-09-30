"use client";

import { useEffect, useState } from "react";
import { ChevronDown, LoaderCircle } from "lucide-react";
import { ApiError, getPerformanceOverview } from "@/lib/api";
import {
  DIAGNOSIS_LABELS,
  EVIDENCE_LABELS,
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
  type DiagnosisKey,
  type MetricKey,
  type PerformanceOverview,
  type PerformanceScope,
} from "@/lib/performance";
import { formatDateTime } from "@/lib/youtube";
import { cn } from "@/lib/utils";

const DIAGNOSIS_ORDER: DiagnosisKey[] = ["hook", "retention", "engagement", "conversion"];

function Metric({ name, overview, compact = false }: { name: MetricKey; overview: PerformanceOverview; compact?: boolean }) {
  const result = overview.metrics[name];
  const value = formatPerformanceValue(name, result?.value);
  const trend = overview.trends[name];
  const unavailable = value === "—";
  const help = [METRIC_TOOLTIPS[name], result ? `${result.n} Videos${result.missing ? ` · ${result.missing} ohne Wert` : ""}` : null].filter(Boolean).join(" · ");
  return (
    <div className="min-w-0" data-metric={name} title={help || undefined}>
      <dt className="truncate text-[10px] font-bold uppercase tracking-[.07em] text-[var(--muted-foreground)]">{METRIC_LABELS[name]}</dt>
      <dd className="mt-0.5 flex items-baseline gap-1.5">
        <span className={cn("font-semibold tabular-nums", compact ? "text-sm" : "text-base", unavailable && "text-[var(--muted-foreground)]")} aria-label={unavailable ? "Nicht verfügbar" : undefined}>{value}</span>
        {trendLabel(trend) && <span className="mono shrink-0 text-[10px] text-[var(--muted-foreground)]" title={trendTitle(trend)}>{trendLabel(trend)}</span>}
      </dd>
    </div>
  );
}

/** Compact channel summary over one cohort of videos (read from stored analytics only). */
export function ChannelPerformance({ refreshKey = 0 }: { refreshKey?: number }) {
  const [scope, setScope] = useState<PerformanceScope>("last10");
  const [overview, setOverview] = useState<PerformanceOverview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [expanded, setExpanded] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    getPerformanceOverview(scope, controller.signal)
      .then((next) => { setOverview(next); setError(null); })
      .catch((reason) => {
        if (!controller.signal.aborted) setError(reason instanceof ApiError || reason instanceof Error ? reason.message : "Channel Performance konnte nicht geladen werden.");
      })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [scope, refreshKey]);

  const empty = overview !== null && overview.video_count === 0;
  const baselineIsScope = overview?.scope === "all";
  return (
    <section className="workspace-card mt-5 p-4" aria-labelledby="channel-performance-title">
      {/* text-xs on the row: a global `button, select { font: inherit }` rule sets their size. */}
      <div className="flex items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2">
          <h2 id="channel-performance-title" className="whitespace-nowrap text-sm font-semibold">Channel Performance</h2>
          {loading && <LoaderCircle className="size-3 shrink-0 animate-spin text-[var(--muted-foreground)]" aria-label="Lädt" />}
        </div>
        {/* text-[11px] on the label: a global `select { font: inherit }` rule sets the select's size. */}
        <label className="shrink-0 text-[11px]">
          <span className="sr-only">Zeitraum</span>
          <select aria-label="Zeitraum" value={scope} onChange={(event) => { setLoading(true); setScope(event.target.value as PerformanceScope); }} className="h-7 rounded-lg border border-[var(--border)] bg-transparent px-2 font-medium text-[var(--foreground)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#ff6838]/40">
            {SCOPE_OPTIONS.map(([key, label]) => <option key={key} value={key}>{label}</option>)}
          </select>
        </label>
      </div>
      {overview && !empty && <p className="mt-0.5 truncate text-[11px] text-[var(--muted-foreground)]" aria-label="Cohort">{cohortLine(overview, formatDateTime)}</p>}

      {error && <p role="alert" className="mt-2 text-xs text-red-800 dark:text-red-300">{error}</p>}
      {empty && <p className="mt-2 text-xs text-[var(--muted-foreground)]">Noch keine Analytics-Daten für diesen Zeitraum.</p>}

      {overview && !empty && (
        <>
          <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-3 sm:grid-cols-3 lg:grid-cols-5" aria-label="Primary metrics">
            {primaryMetrics(overview).map((name) => <Metric key={name} name={name} overview={overview} />)}
          </dl>

          <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 border-t border-[var(--border)] pt-2.5 text-[11px]" aria-label="Diagnosis">
            {DIAGNOSIS_ORDER.map((key) => {
              const entry = overview.diagnosis[key];
              const status = entry?.status;
              return (
                <span key={key} className="whitespace-nowrap" data-diagnosis={key} title={entry ? `${EVIDENCE_LABELS[entry.evidence] ?? entry.evidence} · verglichen mit ${entry.baseline_n} anderen Videos deines Kanals` : undefined}>
                  <span className="font-semibold">{DIAGNOSIS_LABELS[key]}</span>{" "}
                  <span className={cn("text-[var(--muted-foreground)]", status === "weaker" && "text-amber-800 dark:text-amber-300")}>
                    {baselineIsScope && status === "insufficient_data" ? "—" : diagnosisLabel(status)}
                  </span>
                </span>
              );
            })}
            {baselineIsScope && <span className="text-[10px] text-[var(--muted-foreground)]">Kanal-Vergleich nur für einen Zeitraum, nicht für „Alle“.</span>}
          </div>
          {overview.main_signal && (
            <p className="mt-1.5 text-xs" aria-label="Main signal"><span className="font-semibold">Auffällig:</span> {overview.main_signal.message}</p>
          )}

          <div className="mt-2 text-[11px]">
            <button type="button" className="interactive-text -ml-2" aria-expanded={expanded} aria-controls="channel-performance-more" onClick={() => setExpanded((value) => !value)}>
              {expanded ? "Weniger anzeigen" : "Mehr anzeigen"} <ChevronDown className={cn("size-3 transition-transform", expanded && "rotate-180")} />
            </button>
          </div>
          {expanded && (
            <dl id="channel-performance-more" className="mt-2 grid grid-cols-2 gap-x-4 gap-y-2.5 sm:grid-cols-3 lg:grid-cols-6" aria-label="Secondary metrics">
              {secondaryMetrics(overview).map((name) => <Metric key={name} name={name} overview={overview} compact />)}
            </dl>
          )}
          {expanded && <p className="mt-2 text-[10px] text-[var(--muted-foreground)]">Swipe-away: nicht verfügbar – {overview.swipe_away.reason}</p>}
        </>
      )}
    </section>
  );
}
