"use client";

import { ArrowRight, ChevronDown, LoaderCircle, PencilLine, RefreshCw, Sparkles, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  canGenerateTopic,
  directionArrow,
  formatTopicScore,
  topicDetailRows,
  topicLoadingLabel,
  type TopicFlowAction,
  type TopicFlowState,
} from "@/lib/topic-intelligence";

type TopicProposalProps = {
  state: TopicFlowState;
  dispatch: (action: TopicFlowAction) => void;
  onGenerate: () => void;
  onTryAnother: () => void;
  onRetry: () => void;
};

const directionTone = {
  up: "text-[var(--success)]",
  neutral: "text-[var(--muted-foreground)]",
  down: "text-[var(--warning)]",
} as const;

/** The proposed next video: shown before any generation cost is spent. */
export function TopicProposal({ state, dispatch, onGenerate, onTryAnother, onRetry }: TopicProposalProps) {
  if (state.phase === "idle") return null;
  const loading = topicLoadingLabel(state);

  if (state.phase === "loading") {
    return (
      <section className="topic-proposal cf-surface mt-4 w-full rounded-[20px] border p-5 text-left" aria-live="polite" aria-busy="true">
        <p className="flex items-center gap-2 text-sm font-medium text-[var(--muted-foreground)]">
          <LoaderCircle className="size-4 animate-spin" /> {loading}
        </p>
      </section>
    );
  }

  if (state.phase === "unavailable" || state.phase === "exhausted" || state.phase === "error") {
    return (
      <section className="topic-proposal cf-surface mt-4 w-full rounded-[20px] border p-5 text-left" role="status" aria-live="polite">
        <div className="flex items-start justify-between gap-3">
          <div>
            <p className="text-sm font-semibold">{state.message}</p>
            <p className="mt-1 text-xs text-[var(--muted-foreground)]">You can still enter your own question above.</p>
          </div>
          <button type="button" aria-label="Close topic suggestion" onClick={() => dispatch({ type: "dismiss" })} className="rounded-full p-1 text-[var(--muted-foreground)] hover:bg-[var(--surface-hover)]"><X className="size-4" /></button>
        </div>
        <Button variant="outline" size="sm" className="mt-3" onClick={onRetry}><RefreshCw className="size-3.5" /> Try again</Button>
      </section>
    );
  }

  const { candidate } = state;
  const editing = state.phase === "proposed" && state.editing;
  const starting = state.phase === "starting";
  const detailsOpen = state.phase === "proposed" && state.detailsOpen;
  return (
    <section className="topic-proposal cf-surface mt-4 w-full rounded-[20px] border p-5 text-left" aria-labelledby="topic-proposal-title" aria-live="polite">
      <div className="flex items-start justify-between gap-3">
        <p className="flex items-center gap-1.5 text-[11px] font-bold uppercase tracking-[.12em] text-[var(--accent)]"><Sparkles className="size-3.5" /> Proposed next video</p>
        <button type="button" aria-label="Close topic suggestion" disabled={starting} onClick={() => dispatch({ type: "dismiss" })} className="rounded-full p-1 text-[var(--muted-foreground)] hover:bg-[var(--surface-hover)] disabled:opacity-50"><X className="size-4" /></button>
      </div>
      {editing ? (
        <textarea
          aria-label="Edit topic"
          autoFocus
          rows={2}
          value={state.draft}
          onChange={(event) => dispatch({ type: "draft", value: event.target.value })}
          className="mt-2 block w-full resize-none rounded-xl border bg-transparent px-3 py-2 text-lg font-semibold leading-7 outline-none focus:border-[#ff6838]"
        />
      ) : (
        <h2 id="topic-proposal-title" className="balance mt-2 text-xl font-semibold leading-7 tracking-[-0.01em]">{candidate.question}</h2>
      )}
      {candidate.explanation.length > 0 && (
        <div className="mt-4">
          <p className="text-[11px] font-bold uppercase tracking-[.12em] text-[#85857c]">Why this topic:</p>
          <ul className="mt-1.5 grid gap-1 text-sm">
            {candidate.explanation.map((line) => (
              <li key={`${line.signal}-${line.label}`} className="flex items-center gap-2">
                <span aria-hidden className={`mono w-4 text-center font-bold ${directionTone[line.direction]}`}>{directionArrow(line.direction)}</span>
                <span>{line.label}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
      <p className="mt-3 text-xs leading-5 text-[var(--muted-foreground)]">{candidate.rationale} <span className="whitespace-nowrap">· Confidence: {candidate.confidence}</span></p>
      {state.phase === "proposed" && state.error && <p role="alert" className="mt-3 text-sm font-medium text-[var(--destructive)]">{state.error}</p>}
      <div className="mt-4 flex flex-wrap items-center gap-2">
        <Button variant="accent" size="sm" onClick={onGenerate} disabled={starting || !canGenerateTopic(state)}>
          {starting ? <><LoaderCircle className="size-3.5 animate-spin" /> {loading}</> : <>Generate video <ArrowRight className="size-3.5" /></>}
        </Button>
        <Button variant="outline" size="sm" onClick={onTryAnother} disabled={starting}><RefreshCw className="size-3.5" /> Try another</Button>
        {editing ? (
          <Button variant="ghost" size="sm" onClick={() => dispatch({ type: "cancelEdit" })} disabled={starting}>Cancel edit</Button>
        ) : (
          <Button variant="ghost" size="sm" onClick={() => dispatch({ type: "edit" })} disabled={starting}><PencilLine className="size-3.5" /> Edit topic</Button>
        )}
        <button type="button" aria-expanded={detailsOpen} aria-controls="topic-proposal-details" disabled={starting} onClick={() => dispatch({ type: "toggleDetails" })} className="ml-auto flex items-center gap-1 text-xs font-semibold text-[var(--muted-foreground)] hover:text-[var(--foreground)]">
          Details <ChevronDown className={`size-3.5 transition-transform ${detailsOpen ? "rotate-180" : ""}`} />
        </button>
      </div>
      {detailsOpen && (
        <div id="topic-proposal-details" className="mt-3 overflow-x-auto rounded-xl border p-3 text-xs">
          <table className="w-full min-w-[320px] text-left">
            <thead className="text-[#85857c]"><tr><th className="py-1 font-semibold">Signal</th><th className="font-semibold">Value</th><th className="font-semibold">Confidence</th><th className="font-semibold">Weight</th></tr></thead>
            <tbody>
              {topicDetailRows(candidate).map((row) => (
                <tr key={row.signal} className="border-t border-black/5 dark:border-white/5"><td className="py-1">{row.label}</td><td className="mono">{row.value}</td><td>{row.confidence}</td><td className="mono">{row.weight}</td></tr>
              ))}
            </tbody>
          </table>
          <p className="mt-2 text-[var(--muted-foreground)]">Score {formatTopicScore(candidate.final_score)} · {candidate.score_version} · Sources: {candidate.details.sources.join(", ") || "none"}</p>
        </div>
      )}
    </section>
  );
}
