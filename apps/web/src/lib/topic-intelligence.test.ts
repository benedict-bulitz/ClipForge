import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import {
  DISCOVERY_UNAVAILABLE,
  INITIAL_TOPIC_FLOW,
  canGenerateTopic,
  directionArrow,
  finalTopicQuestion,
  formatTopicScore,
  isTopicFlowBusy,
  topicDetailRows,
  topicFlowReducer,
  topicGenerationSource,
  topicLoadingLabel,
  type TopicCandidate,
  type TopicFlowState,
  type TopicProposal,
} from "./topic-intelligence.ts";

const home = readFileSync(new URL("../app/page.tsx", import.meta.url), "utf8");
const component = readFileSync(new URL("../components/topic-proposal.tsx", import.meta.url), "utf8");
const api = readFileSync(new URL("./api.ts", import.meta.url), "utf8");

function candidate(overrides: Partial<TopicCandidate> = {}): TopicCandidate {
  return {
    candidate_id: "tc_1",
    question: "Warum haben wir plötzlich Lust auf Süßes?",
    topic: "Heißhunger",
    rationale: "German Wikipedia interest is 4x its usual level.",
    niche: "essen_trinken",
    language: "de",
    region: "DE",
    final_score: 0.734,
    confidence: "medium",
    score_version: "ti-score-v1",
    status: "proposed",
    explanation: [
      { direction: "up", label: "Recent interest", signal: "trend", confidence: "high" },
      { direction: "up", label: "Strong novelty", signal: "novelty", confidence: "high" },
      { direction: "up", label: "Good visual potential", signal: "visual", confidence: "medium" },
      { direction: "neutral", label: "Moderate competition", signal: "competition", confidence: "medium" },
    ],
    details: {
      components: {
        trend: { value: 0.9, confidence: "high", effective: 0.9, weight: 0.2, contribution: 0.18 },
        own_performance: { value: null, confidence: "unavailable", effective: 0.5, weight: 0.04, contribution: 0.02 },
      },
      penalties: { flags: [], value: 0 },
      sources: ["wikipedia_pageviews"],
      transformation: "llm",
    },
    discovered_at: null,
    freshness_at: null,
    ...overrides,
  };
}

function proposal(overrides: Partial<TopicProposal> = {}): TopicProposal {
  return { status: "proposed", message: null, candidate: candidate(), pool: { status: "ok", remaining: 3, sources: [] }, ...overrides };
}

function run(...actions: Parameters<typeof topicFlowReducer>[1][]): TopicFlowState {
  return actions.reduce(topicFlowReducer, INITIAL_TOPIC_FLOW);
}

test("Generate Next Video button exists next to the unchanged manual composer", () => {
  assert.match(home, />?\s*Generate Next Video/);
  assert.match(home, /onClick=\{\(\) => void discoverTopic\(\)\}/);
  assert.match(home, /proposeNextTopic\(\)/);
  // Manual input stays exactly as it was: same textarea, same Generate button, same call.
  assert.match(home, /placeholder="What should ClipForge create\?"/);
  assert.match(home, /startGeneration\(prompt, options\)/);
  assert.match(home, /Multiple questions/);
  assert.match(home, /submitQuestionsInOrder\(questions, \(question\) => startGeneration\(question, options\)\)/);
});

test("clicking discovers first: a loading state, then a proposal, never an immediate generation", () => {
  const loading = run({ type: "discover" });
  assert.equal(loading.phase, "loading");
  assert.equal(isTopicFlowBusy(loading), true);
  assert.equal(topicLoadingLabel(loading), "Finding the next topic…");
  const shown = topicFlowReducer(loading, { type: "received", proposal: proposal() });
  assert.equal(shown.phase, "proposed");
  assert.equal(finalTopicQuestion(shown), "Warum haben wir plötzlich Lust auf Süßes?");
  const discover = home.slice(home.indexOf("async function discoverTopic"), home.indexOf("async function tryAnother"));
  assert.doesNotMatch(discover, /startGeneration/);
  assert.match(component, /aria-busy="true"/);
  assert.match(component, /LoaderCircle/);
});

test("the proposed topic is shown with a compact rationale and optional details", () => {
  assert.match(component, /Proposed next video/);
  assert.match(component, /candidate\.question/);
  assert.match(component, /Why this topic:/);
  assert.match(component, /candidate\.explanation\.map/);
  assert.match(component, /directionArrow\(line\.direction\)/);
  assert.match(component, /candidate\.rationale/);
  assert.match(component, />\s*Details/);
  assert.match(component, /aria-expanded=\{detailsOpen\}/);
  assert.equal(directionArrow("up"), "↑");
  assert.equal(directionArrow("neutral"), "→");
  assert.equal(directionArrow("down"), "↓");
  const opened = topicFlowReducer(run({ type: "discover" }, { type: "received", proposal: proposal() }), { type: "toggleDetails" });
  assert.equal(opened.phase === "proposed" && opened.detailsOpen, true);
  const rows = topicDetailRows(candidate());
  assert.deepEqual(rows.map((row) => [row.label, row.value, row.weight]), [["Current interest", "90", "20%"], ["Your channel's history", "not available", "4%"]]);
  assert.equal(formatTopicScore(0.734), "73");
});

test("Generate video hands the (possibly edited) question to the one generation entry point", () => {
  const shown = run({ type: "discover" }, { type: "received", proposal: proposal() });
  const starting = topicFlowReducer(shown, { type: "generate" });
  assert.equal(starting.phase, "starting");
  assert.deepEqual(topicGenerationSource(candidate()), { topic_source: "topic_intelligence", topic_candidate_id: "tc_1" });
  assert.equal(topicFlowReducer(starting, { type: "started" }).phase, "idle");
  const generate = home.slice(home.indexOf("async function generateTopic"), home.indexOf("async function removeFromQueue"));
  assert.match(generate, /startGeneration\(question, options, source\)/);
  assert.match(generate, /openWhenComplete\(started\)/);
  assert.match(api, /body: JSON\.stringify\(\{ prompt, mode: "auto", options, \.\.\.\(topic \?\? \{\}\) \}\)/);
  assert.equal((api.match(/"\/generation-jobs", \{\s*method: "POST"/g) ?? []).length, 1);
  assert.match(component, />Generate video </);
});

test("a failed start keeps the proposal and shows the error", () => {
  const shown = run({ type: "discover" }, { type: "received", proposal: proposal() }, { type: "edit" }, { type: "draft", value: "Warum lieben wir Zucker?" });
  const failed = topicFlowReducer(topicFlowReducer(shown, { type: "generate" }), { type: "failed", message: "API down" });
  assert.equal(failed.phase, "proposed");
  assert.equal(failed.phase === "proposed" && failed.error, "API down");
  assert.equal(finalTopicQuestion(failed), "Warum lieben wir Zucker?");
});

test("Try another asks the backend for the next candidate instead of rerunning discovery", () => {
  const shown = run({ type: "discover" }, { type: "received", proposal: proposal() });
  const loading = topicFlowReducer(shown, { type: "another" });
  assert.equal(loading.phase === "loading" && loading.action, "another");
  assert.equal(topicLoadingLabel(loading), "Finding another topic…");
  const next = topicFlowReducer(loading, { type: "received", proposal: proposal({ candidate: candidate({ candidate_id: "tc_2", question: "Wie entstehen Polarlichter?" }) }) });
  assert.equal(next.phase === "proposed" && next.candidate.candidate_id, "tc_2");
  assert.match(api, /\/topic-intelligence\/candidates\/\$\{encodeURIComponent\(candidateId\)\}\/skip/);
  assert.match(home, /tryAnotherTopic\(candidateId\)/);
  assert.match(component, /Try another/);
  const exhausted = topicFlowReducer(topicFlowReducer(next, { type: "another" }), { type: "received", proposal: proposal({ status: "exhausted", candidate: null, message: null }) });
  assert.equal(exhausted.phase, "exhausted");
});

test("Edit topic changes only the question; an empty edit cannot be generated", () => {
  const editing = run({ type: "discover" }, { type: "received", proposal: proposal() }, { type: "edit" });
  assert.equal(editing.phase === "proposed" && editing.editing, true);
  const edited = topicFlowReducer(editing, { type: "draft", value: "  Warum   mögen wir Süßes?  " });
  assert.equal(finalTopicQuestion(edited), "Warum mögen wir Süßes?");
  const empty = topicFlowReducer(editing, { type: "draft", value: " " });
  assert.equal(canGenerateTopic(empty), false);
  assert.equal(topicFlowReducer(empty, { type: "generate" }), empty);
  const cancelled = topicFlowReducer(edited, { type: "cancelEdit" });
  assert.equal(finalTopicQuestion(cancelled), "Warum haben wir plötzlich Lust auf Süßes?");
  assert.match(component, /aria-label="Edit topic"/);
  assert.match(component, /Edit topic/);
});

test("source failure shows a clear unavailable state and manual input keeps working", () => {
  const unavailable = run({ type: "discover" }, {
    type: "received",
    proposal: { status: "unavailable", message: null, candidate: null, pool: { sources: [{ name: "wikipedia_pageviews", status: "failed", error: "HTTP 503" }] } },
  });
  assert.equal(unavailable.phase, "unavailable");
  assert.equal(unavailable.phase === "unavailable" && unavailable.message, DISCOVERY_UNAVAILABLE);
  assert.deepEqual(unavailable.phase === "unavailable" && unavailable.partialSources, ["wikipedia_pageviews"]);
  assert.equal(DISCOVERY_UNAVAILABLE, "Topic discovery is temporarily unavailable.");
  assert.match(component, /You can still enter your own question above\./);
  const networkError = topicFlowReducer(run({ type: "discover" }), { type: "failed", message: "Could not reach the ClipForge API." });
  assert.equal(networkError.phase, "error");
  // The manual Generate button does not depend on the topic flow's state.
  const manualButton = home.slice(home.indexOf('<Button variant="accent" onClick={() => void generate()}'), home.indexOf("</Button>", home.indexOf('<Button variant="accent" onClick={() => void generate()}')));
  assert.doesNotMatch(manualButton, /topicFlow/);
});

test("busy states ignore duplicate clicks", () => {
  const loading = run({ type: "discover" });
  assert.equal(topicFlowReducer(loading, { type: "discover" }), loading);
  assert.equal(topicFlowReducer(loading, { type: "dismiss" }), loading);
  const starting = topicFlowReducer(topicFlowReducer(loading, { type: "received", proposal: proposal() }), { type: "generate" });
  assert.equal(topicFlowReducer(starting, { type: "another" }), starting);
  assert.match(home, /disabled=\{isTopicFlowBusy\(topicFlow\)\}/);
});
