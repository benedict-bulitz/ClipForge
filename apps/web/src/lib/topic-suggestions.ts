/**
 * Home topic suggestions: 3 live Topic Intelligence questions as chips.
 *
 * - Visible chips change ONLY on a user action: clicking a chip (that one slot
 *   is replaced) or "Neue Vorschläge" (all three).  Polling, rerenders, focus
 *   and navigation never reshuffle them; the visible set is persisted.
 * - A hidden reserve makes replacements instant; it is refilled in the
 *   background from the backend's candidate pool (which owns discovery,
 *   scoring, caching and quota limits).
 * - A chip only fills the textarea.  Generation starts solely from the
 *   existing Generate button, and never waits for topic discovery.
 */

export type TopicSuggestion = {
  candidate_id: string;
  question: string;
  rationale: string;
  confidence: string;
  /** Client receipt time (ms), for background reserve freshness only. */
  fetched_at: number;
};

export type TopicSuggestionsResponse = {
  status: "ok" | "exhausted" | "unavailable";
  message: string | null;
  candidates: { candidate_id: string; question: string; rationale?: string; confidence?: string }[];
};

export type TopicSuggestionsRequest = {
  count: number;
  exclude: string[];
  picked: string[];
  dismissed: string[];
};

/** What the existing generation entry point needs to attach provenance. */
export type TopicGenerationSource = {
  topic_source: "topic_intelligence";
  topic_candidate_id: string;
};

export type SuggestionStatus = "idle" | "loading" | "ready" | "unavailable";

export type SuggestionState = {
  visible: (TopicSuggestion | null)[];
  reserve: TopicSuggestion[];
  /** Recently displayed candidate ids (never shown again soon). */
  recent: string[];
  status: SuggestionStatus;
  message: string | null;
};

export const VISIBLE_COUNT = 3;
export const RESERVE_TARGET = 6;
export const RECENT_LIMIT = 60;
/** Hidden reserve entries older than this are refetched; visible chips are never replaced by time. */
export const RESERVE_TTL_MS = 6 * 60 * 60 * 1000;
export const TOPIC_SUGGESTIONS_KEY = "clipforge-topic-suggestions";
export const TOPIC_SUGGESTIONS_VERSION = 1;
export const DISCOVERY_UNAVAILABLE = "Topic discovery is temporarily unavailable.";

export function emptySuggestions(): SuggestionState {
  return { visible: Array.from({ length: VISIBLE_COUNT }, () => null), reserve: [], recent: [], status: "idle", message: null };
}

function normalized(question: string): string {
  return question.toLocaleLowerCase("de").replace(/[^\p{L}\p{N}]+/gu, " ").trim();
}

function remember(recent: string[], ids: string[]): string[] {
  return [...ids, ...recent.filter((id) => !ids.includes(id))].slice(0, RECENT_LIMIT);
}

export function shownSuggestions(state: SuggestionState): TopicSuggestion[] {
  return [...state.visible.filter((item): item is TopicSuggestion => item !== null), ...state.reserve];
}

/** Add fetched candidates: empty visible slots first, then the reserve. No duplicates of anything shown or recent. */
export function mergeSuggestions(state: SuggestionState, candidates: TopicSuggestionsResponse["candidates"], now: number): SuggestionState {
  const shown = shownSuggestions(state);
  const ids = new Set([...shown.map((item) => item.candidate_id), ...state.recent]);
  const questions = new Set(shown.map((item) => normalized(item.question)));
  const fresh: TopicSuggestion[] = [];
  for (const candidate of candidates) {
    const question = String(candidate.question || "").trim();
    const key = normalized(question);
    if (!candidate.candidate_id || !question || ids.has(candidate.candidate_id) || questions.has(key)) continue;
    ids.add(candidate.candidate_id);
    questions.add(key);
    fresh.push({ candidate_id: candidate.candidate_id, question, rationale: candidate.rationale ?? "", confidence: candidate.confidence ?? "low", fetched_at: now });
  }
  const visible = [...state.visible];
  const shownNow: string[] = [];
  for (let index = 0; index < visible.length && fresh.length; index += 1) {
    if (visible[index] === null) {
      visible[index] = fresh.shift()!;
      shownNow.push(visible[index]!.candidate_id);
    }
  }
  const reserve = [...state.reserve, ...fresh].slice(0, RESERVE_TARGET);
  return { ...state, visible, reserve, recent: remember(state.recent, shownNow), status: "ready", message: null };
}

/** Click: that slot alone gets the next reserve candidate (instantly); the others stay. */
export function pickSuggestion(state: SuggestionState, index: number): { state: SuggestionState; picked: TopicSuggestion | null } {
  const picked = state.visible[index] ?? null;
  if (!picked) return { state, picked: null };
  const visible = [...state.visible];
  const [next, ...reserve] = state.reserve;
  visible[index] = next ?? null;
  return { state: { ...state, visible, reserve, recent: remember(state.recent, next ? [next.candidate_id, picked.candidate_id] : [picked.candidate_id]) }, picked };
}

/** "Neue Vorschläge": all three visible chips are replaced from the reserve. */
export function refreshAllSuggestions(state: SuggestionState): { state: SuggestionState; dismissed: string[] } {
  const dismissed = state.visible.filter((item): item is TopicSuggestion => item !== null).map((item) => item.candidate_id);
  const next = state.reserve.slice(0, VISIBLE_COUNT);
  const visible = Array.from({ length: VISIBLE_COUNT }, (_unused, index) => next[index] ?? null);
  return {
    state: { ...state, visible, reserve: state.reserve.slice(VISIBLE_COUNT), recent: remember(state.recent, [...next.map((item) => item.candidate_id), ...dismissed]) },
    dismissed,
  };
}

/** How many to fetch in the background, or null when visible + reserve are full. */
export function refillRequest(state: SuggestionState): { count: number; exclude: string[] } | null {
  const missing = state.visible.filter((item) => item === null).length + Math.max(0, RESERVE_TARGET - state.reserve.length);
  if (missing <= 0) return null;
  const exclude = [...new Set([...shownSuggestions(state).map((item) => item.candidate_id), ...state.recent])];
  return { count: Math.min(12, missing), exclude };
}

/** Drop only stale HIDDEN reserve entries; visible chips are kept as they are. */
export function expireReserve(state: SuggestionState, now: number): SuggestionState {
  const reserve = state.reserve.filter((item) => now - item.fetched_at < RESERVE_TTL_MS);
  return reserve.length === state.reserve.length ? state : { ...state, reserve };
}

export function serializeSuggestions(state: SuggestionState): string {
  return JSON.stringify({ version: TOPIC_SUGGESTIONS_VERSION, visible: state.visible, reserve: state.reserve, recent: state.recent });
}

function parseSuggestion(value: unknown): TopicSuggestion | null {
  if (!value || typeof value !== "object") return null;
  const item = value as Record<string, unknown>;
  if (typeof item.candidate_id !== "string" || typeof item.question !== "string" || !item.question.trim()) return null;
  return {
    candidate_id: item.candidate_id,
    question: item.question,
    rationale: typeof item.rationale === "string" ? item.rationale : "",
    confidence: typeof item.confidence === "string" ? item.confidence : "low",
    fetched_at: typeof item.fetched_at === "number" ? item.fetched_at : 0,
  };
}

export function parseSuggestions(raw: string | null): SuggestionState {
  const state = emptySuggestions();
  if (!raw) return state;
  try {
    const value = JSON.parse(raw) as Record<string, unknown>;
    if (value.version !== TOPIC_SUGGESTIONS_VERSION) return state;
    const visible = Array.isArray(value.visible) ? value.visible.slice(0, VISIBLE_COUNT).map(parseSuggestion) : [];
    while (visible.length < VISIBLE_COUNT) visible.push(null);
    const reserve = (Array.isArray(value.reserve) ? value.reserve.map(parseSuggestion) : []).filter((item): item is TopicSuggestion => item !== null);
    const recent = Array.isArray(value.recent) ? value.recent.filter((id): id is string => typeof id === "string").slice(0, RECENT_LIMIT) : [];
    return { ...state, visible, reserve: reserve.slice(0, RESERVE_TARGET), recent, status: visible.some(Boolean) ? "ready" : "idle" };
  } catch {
    return state;
  }
}

export function topicGenerationSource(topic: TopicSuggestion | null, prompt: string): TopicGenerationSource | undefined {
  if (!topic || !prompt.trim()) return undefined;
  return { topic_source: "topic_intelligence", topic_candidate_id: topic.candidate_id };
}

export type SuggestionStorage = { load: () => string | null; save: (value: string) => void };

export type TopicSuggestionsOptions = {
  load: (request: TopicSuggestionsRequest) => Promise<TopicSuggestionsResponse>;
  onChange: (state: SuggestionState) => void;
  storage?: SuggestionStorage;
  now?: () => number;
};

export type TopicSuggestions = {
  start: () => void;
  stop: () => void;
  /** Chip click: returns the suggestion for the textarea; replaces only that slot. */
  pick: (index: number) => TopicSuggestion | null;
  /** "Neue Vorschläge": replaces all three visible chips. */
  refreshAll: () => void;
  /** Background refill of empty slots and the hidden reserve (single-flight). */
  refill: () => Promise<void>;
  readonly state: SuggestionState;
};

export function browserSuggestionStorage(): SuggestionStorage {
  return {
    load: () => {
      try {
        return typeof window === "undefined" ? null : window.localStorage.getItem(TOPIC_SUGGESTIONS_KEY);
      } catch {
        return null;
      }
    },
    save: (value) => {
      try {
        if (typeof window !== "undefined") window.localStorage.setItem(TOPIC_SUGGESTIONS_KEY, value);
      } catch {
        // Persistence is a convenience; suggestions still work without it.
      }
    },
  };
}

export function createTopicSuggestions(options: TopicSuggestionsOptions): TopicSuggestions {
  const now = options.now ?? (() => Date.now());
  const storage = options.storage;
  let state = emptySuggestions();
  let running = false;
  let inFlight: Promise<void> | null = null;
  let again = false;
  let pendingPicked: string[] = [];
  let pendingDismissed: string[] = [];

  function update(next: SuggestionState) {
    state = next;
    storage?.save(serializeSuggestions(state));
    if (running) options.onChange(state);
  }

  async function refillOnce(): Promise<void> {
    const request = refillRequest(state);
    const picked = pendingPicked;
    const dismissed = pendingDismissed;
    if (!request && !picked.length && !dismissed.length) return;
    pendingPicked = [];
    pendingDismissed = [];
    if (!shownSuggestions(state).length) update({ ...state, status: "loading" });
    try {
      const exclude = request?.exclude ?? [...new Set([...shownSuggestions(state).map((item) => item.candidate_id), ...state.recent])];
      const response = await options.load({ count: request?.count ?? 1, exclude, picked, dismissed });
      if (!running) return;
      if (response.status === "unavailable" && !response.candidates.length) {
        update({ ...state, status: state.visible.some(Boolean) ? state.status : "unavailable", message: response.message || DISCOVERY_UNAVAILABLE });
        return;
      }
      update(mergeSuggestions(state, response.candidates, now()));
    } catch {
      pendingPicked = [...picked, ...pendingPicked];
      pendingDismissed = [...dismissed, ...pendingDismissed];
      if (running && !state.visible.some(Boolean)) update({ ...state, status: "unavailable", message: DISCOVERY_UNAVAILABLE });
    }
  }

  async function refill(): Promise<void> {
    if (!running) return;
    if (inFlight) {
      again = true;
      return inFlight;
    }
    inFlight = (async () => {
      do {
        again = false;
        await refillOnce();
      } while (again && running);
    })().finally(() => {
      inFlight = null;
    });
    return inFlight;
  }

  return {
    start() {
      if (running) return;
      running = true;
      state = expireReserve(parseSuggestions(storage?.load() ?? null), now());
      options.onChange(state);
      void refill();
    },
    stop() {
      running = false;
    },
    pick(index) {
      const result = pickSuggestion(state, index);
      if (!result.picked) return null;
      pendingPicked.push(result.picked.candidate_id);
      update(result.state);
      void refill();
      return result.picked;
    },
    refreshAll() {
      const result = refreshAllSuggestions(state);
      pendingDismissed.push(...result.dismissed);
      update(result.state);
      void refill();
    },
    refill,
    get state() {
      return state;
    },
  };
}
