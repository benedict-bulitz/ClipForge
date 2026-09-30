/**
 * Generate Next Video: client state for one proposed topic.
 *
 * The backend discovers, scores and explains candidates (its scoring
 * authority is the only place a score is computed); this module only models
 * the user-confirmed flow: discover → proposed → (Generate video | Try another
 * | Edit topic).  Nothing here starts generation on its own.
 */

export type TopicDirection = "up" | "neutral" | "down";

export type TopicExplanationLine = {
  direction: TopicDirection;
  label: string;
  signal: string;
  confidence: string;
};

export type TopicComponent = {
  value: number | null;
  confidence: string;
  effective: number;
  weight: number;
  contribution: number;
};

export type TopicCandidate = {
  candidate_id: string;
  question: string;
  topic: string;
  rationale: string;
  niche: string;
  language: string;
  region: string;
  final_score: number;
  confidence: string;
  score_version: string;
  status: string;
  explanation: TopicExplanationLine[];
  details: {
    components: Record<string, TopicComponent>;
    penalties: { flags?: string[]; value?: number };
    sources: string[];
    transformation: string | null;
  };
  discovered_at: string | null;
  freshness_at: string | null;
};

export type TopicPoolSource = { name: string; status: string; error: string | null };

export type TopicProposal = {
  status: "proposed" | "unavailable" | "exhausted";
  message: string | null;
  candidate: TopicCandidate | null;
  pool: { status?: string; remaining?: number; sources?: TopicPoolSource[] } | null;
};

/** What the existing generation entry point needs to attach provenance. */
export type TopicGenerationSource = {
  topic_source: "topic_intelligence";
  topic_candidate_id: string;
};

export const DISCOVERY_UNAVAILABLE = "Topic discovery is temporarily unavailable.";
export const DISCOVERY_EXHAUSTED = "No further topic candidates right now. Try again later or enter your own question.";

export type TopicFlowState =
  | { phase: "idle" }
  | { phase: "loading"; action: "discover" | "another"; previous: TopicCandidate | null }
  | { phase: "proposed"; candidate: TopicCandidate; editing: boolean; draft: string; detailsOpen: boolean; error: string | null }
  | { phase: "starting"; candidate: TopicCandidate; question: string; draft: string }
  | { phase: "unavailable"; message: string; partialSources: string[] }
  | { phase: "exhausted"; message: string }
  | { phase: "error"; message: string };

export type TopicFlowAction =
  | { type: "discover" }
  | { type: "another" }
  | { type: "received"; proposal: TopicProposal }
  | { type: "failed"; message: string }
  | { type: "edit" }
  | { type: "draft"; value: string }
  | { type: "cancelEdit" }
  | { type: "toggleDetails" }
  | { type: "generate" }
  | { type: "started" }
  | { type: "dismiss" };

export const INITIAL_TOPIC_FLOW: TopicFlowState = { phase: "idle" };

export function isTopicFlowBusy(state: TopicFlowState): boolean {
  return state.phase === "loading" || state.phase === "starting";
}

function proposed(candidate: TopicCandidate): TopicFlowState {
  return { phase: "proposed", candidate, editing: false, draft: candidate.question, detailsOpen: false, error: null };
}

export function topicFlowReducer(state: TopicFlowState, action: TopicFlowAction): TopicFlowState {
  switch (action.type) {
    case "discover":
      return isTopicFlowBusy(state) ? state : { phase: "loading", action: "discover", previous: null };
    case "another":
      return state.phase === "proposed" ? { phase: "loading", action: "another", previous: state.candidate } : state;
    case "received": {
      if (state.phase !== "loading") return state;
      const { proposal } = action;
      if (proposal.status === "proposed" && proposal.candidate) return proposed(proposal.candidate);
      if (proposal.status === "exhausted") return { phase: "exhausted", message: proposal.message || DISCOVERY_EXHAUSTED };
      return {
        phase: "unavailable",
        message: proposal.message || DISCOVERY_UNAVAILABLE,
        partialSources: (proposal.pool?.sources ?? []).filter((source) => source.status === "failed").map((source) => source.name),
      };
    }
    case "failed":
      if (state.phase === "starting") {
        return { phase: "proposed", candidate: state.candidate, editing: state.draft !== state.candidate.question, draft: state.draft, detailsOpen: false, error: action.message };
      }
      return state.phase === "loading" ? { phase: "error", message: action.message } : state;
    case "edit":
      return state.phase === "proposed" ? { ...state, editing: true, error: null } : state;
    case "draft":
      return state.phase === "proposed" && state.editing ? { ...state, draft: action.value } : state;
    case "cancelEdit":
      return state.phase === "proposed" ? { ...state, editing: false, draft: state.candidate.question } : state;
    case "toggleDetails":
      return state.phase === "proposed" ? { ...state, detailsOpen: !state.detailsOpen } : state;
    case "generate":
      if (state.phase !== "proposed" || !canGenerateTopic(state)) return state;
      return { phase: "starting", candidate: state.candidate, question: finalTopicQuestion(state), draft: state.draft };
    case "started":
      return state.phase === "starting" ? INITIAL_TOPIC_FLOW : state;
    case "dismiss":
      return isTopicFlowBusy(state) ? state : INITIAL_TOPIC_FLOW;
    default:
      return state;
  }
}

/** The question that will be generated: the edited draft, else the proposal. */
export function finalTopicQuestion(state: TopicFlowState): string {
  if (state.phase === "starting") return state.question;
  if (state.phase !== "proposed") return "";
  const draft = state.editing ? state.draft : state.candidate.question;
  return draft.split(/\s+/).filter(Boolean).join(" ");
}

export function canGenerateTopic(state: TopicFlowState): boolean {
  return state.phase === "proposed" && finalTopicQuestion(state).length >= 3;
}

export function topicGenerationSource(candidate: TopicCandidate): TopicGenerationSource {
  return { topic_source: "topic_intelligence", topic_candidate_id: candidate.candidate_id };
}

export function directionArrow(direction: TopicDirection): "↑" | "→" | "↓" {
  return direction === "up" ? "↑" : direction === "down" ? "↓" : "→";
}

export const SIGNAL_LABELS: Record<string, string> = {
  trend: "Current interest",
  outlier: "Outlier evidence",
  competition: "Competition (saturation)",
  novelty: "Novelty",
  channel_fit: "Channel fit",
  suitability: "Knowledge-short fit",
  visual: "Visual potential",
  researchability: "Researchability",
  own_performance: "Your channel's history",
};

export type TopicDetailRow = { signal: string; label: string; value: string; confidence: string; weight: string };

/** Display rows for "Details": the backend's own numbers, formatted, never recomputed. */
export function topicDetailRows(candidate: TopicCandidate): TopicDetailRow[] {
  return Object.entries(candidate.details.components).map(([signal, item]) => ({
    signal,
    label: SIGNAL_LABELS[signal] ?? signal,
    value: item.value === null || item.confidence === "unavailable" ? "not available" : hundredths(item.value),
    confidence: item.confidence,
    weight: `${hundredths(item.weight)}%`,
  }));
}

/** 0..1 shown as 0..100 (display formatting only; the backend owns every score). */
function hundredths(value: number): string {
  return `${Math.round(Math.max(0, Math.min(1, value)) * 100)}`;
}

export function formatTopicScore(score: number): string {
  return hundredths(score);
}

export function topicLoadingLabel(state: TopicFlowState): string | null {
  if (state.phase === "loading") return state.action === "another" ? "Finding another topic…" : "Finding the next topic…";
  if (state.phase === "starting") return "Starting…";
  return null;
}
