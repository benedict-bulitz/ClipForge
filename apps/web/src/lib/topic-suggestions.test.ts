import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import {
  DISCOVERY_UNAVAILABLE,
  RESERVE_TARGET,
  RESERVE_TTL_MS,
  createTopicSuggestions,
  emptySuggestions,
  expireReserve,
  mergeSuggestions,
  parseSuggestions,
  pickSuggestion,
  refillRequest,
  refreshAllSuggestions,
  serializeSuggestions,
  topicGenerationSource,
  type SuggestionState,
  type TopicSuggestionsRequest,
  type TopicSuggestionsResponse,
} from "./topic-suggestions.ts";

const home = readFileSync(new URL("../app/page.tsx", import.meta.url), "utf8");
const api = readFileSync(new URL("./api.ts", import.meta.url), "utf8");

const QUESTIONS = [
  "Warum fühle ich mich nach einem Mittagsschlaf schlechter?",
  "Wie entstehen Polarlichter?",
  "Warum fliegen Zugvögel im V?",
  "Warum haben wir plötzlich Lust auf Süßes?",
  "Wie entsteht Hagel im Sommer?",
  "Warum knistert Laub im Herbst?",
  "Wie finden Bienen zurück in ihren Stock?",
  "Warum ist ein Regenbogen rund?",
  "Wie atmen Tintenfische unter Wasser?",
  "Warum leuchten Glühwürmchen?",
  "Wie entsteht ein Gewitter?",
  "Warum brummen Stromleitungen?",
];

function candidate(index: number) {
  return { candidate_id: `tc_${index}`, question: QUESTIONS[index], rationale: `reason ${index}`, confidence: "medium" };
}

/** A backend stand-in that serves the ranked pool minus what the client excludes. */
function fakeBackend(options: { fail?: boolean; status?: TopicSuggestionsResponse["status"] } = {}) {
  const requests: TopicSuggestionsRequest[] = [];
  const skipped = new Set<string>();
  const load = async (request: TopicSuggestionsRequest): Promise<TopicSuggestionsResponse> => {
    requests.push(request);
    if (options.fail) throw new Error("network down");
    if (options.status === "unavailable") return { status: "unavailable", message: DISCOVERY_UNAVAILABLE, candidates: [] };
    request.picked.forEach((id) => skipped.add(id));
    request.dismissed.forEach((id) => skipped.add(id));
    const candidates = QUESTIONS.map((_q, index) => candidate(index))
      .filter((item) => !request.exclude.includes(item.candidate_id) && !skipped.has(item.candidate_id))
      .slice(0, request.count);
    return { status: candidates.length ? "ok" : "exhausted", message: null, candidates };
  };
  return { load, requests };
}

function memoryStorage(initial: string | null = null) {
  let value = initial;
  return { load: () => value, save: (next: string) => { value = next; }, get value() { return value; } };
}

async function started(backend = fakeBackend(), storage = memoryStorage()) {
  const changes: SuggestionState[] = [];
  const controller = createTopicSuggestions({ load: backend.load, onChange: (state) => changes.push(state), storage, now: () => 1_000 });
  controller.start();
  await controller.refill();
  return { controller, changes, backend, storage };
}

function visibleIds(state: SuggestionState) {
  return state.visible.map((item) => item?.candidate_id ?? null);
}

test("Home shows exactly 3 live suggestions and a reserve is prefetched", async () => {
  const { controller, backend } = await started();
  assert.deepEqual(visibleIds(controller.state), ["tc_0", "tc_1", "tc_2"]);
  assert.equal(controller.state.reserve.length, RESERVE_TARGET);
  assert.equal(backend.requests[0].count, 3 + RESERVE_TARGET);
  assert.match(home, /<TopicSuggestionChips state=\{suggestions\}/);
  assert.match(home, /state\.visible\.map/);
});

test("static example chips are gone", () => {
  assert.doesNotMatch(home, /Why did Concorde disappear\?/);
  assert.doesNotMatch(home, /What if Yellowstone erupted tomorrow\?/);
  assert.doesNotMatch(home, /An astronaut wakes alone on a lunar base/);
  assert.doesNotMatch(home, /const examples/);
});

test("clicking a chip returns its question for the textarea and starts no generation", async () => {
  const { controller } = await started();
  const picked = controller.pick(1);
  assert.equal(picked?.question, "Wie entstehen Polarlichter?");
  const apply = home.slice(home.indexOf("function applySuggestion"), home.indexOf("useEffect(() => {", home.indexOf("function applySuggestion")));
  assert.match(apply, /setPrompt\(picked\.question\)/);
  assert.doesNotMatch(apply, /startGeneration|generate\(\)/);
});

test("the clicked slot alone is replaced instantly from the reserve; the other 2 stay", async () => {
  const { controller, backend } = await started();
  const before = visibleIds(controller.state);
  const requestsBefore = backend.requests.length;
  controller.pick(1);
  // Synchronous replacement: no request had to complete first.
  assert.deepEqual(visibleIds(controller.state), [before[0], "tc_3", before[2]]);
  assert.equal(backend.requests.length, requestsBefore + 1); // the background refill only
  await controller.refill();
  assert.deepEqual(visibleIds(controller.state), [before[0], "tc_3", before[2]]);
});

test("the reserve is refilled in the background and reports the pick", async () => {
  const { controller, backend } = await started();
  controller.pick(0);
  await controller.refill();
  const refill = backend.requests.at(-1)!;
  assert.deepEqual(refill.picked, ["tc_0"]);
  assert.ok(refill.exclude.includes("tc_1") && refill.exclude.includes("tc_0"));
  assert.equal(controller.state.reserve.length, RESERVE_TARGET);
});

test("Neue Vorschläge replaces all 3 and never touches the textarea", async () => {
  const { controller, backend } = await started();
  const before = visibleIds(controller.state);
  controller.refreshAll();
  const after = visibleIds(controller.state);
  assert.deepEqual(after, ["tc_3", "tc_4", "tc_5"]);
  assert.ok(after.every((id) => !before.includes(id)));
  await controller.refill();
  assert.deepEqual(backend.requests.at(-1)!.dismissed, before);
  assert.match(home, /Neue Vorschläge/);
  assert.match(home, /onRefreshAll=\{\(\) => suggestionsRef\.current\?\.refreshAll\(\)\}/);
  const chips = home.slice(home.indexOf("function TopicSuggestionChips"), home.indexOf("function bulkDeleteSummary"));
  assert.doesNotMatch(chips, /setPrompt|startGeneration/);
});

test("visible suggestions are stable: background refills and restarts never reshuffle them", async () => {
  const storage = memoryStorage();
  const { controller } = await started(fakeBackend(), storage);
  const shown = visibleIds(controller.state);
  await controller.refill();
  await controller.refill();
  assert.deepEqual(visibleIds(controller.state), shown);
  controller.stop();
  // Returning to Home (remount) restores the same visible set before any request completes.
  const again = createTopicSuggestions({ load: fakeBackend().load, onChange: () => {}, storage, now: () => 2_000 });
  again.start();
  assert.deepEqual(visibleIds(again.state), shown);
  await again.refill();
  assert.deepEqual(visibleIds(again.state), shown);
  // The controller is created once per mount, not per render or poll.
  assert.match(home, /controller\.start\(\);\n    return \(\) => \{\n      controller\.stop\(\);/);
});

test("reserve replacement needs no new discovery; stale reserve entries are dropped in the background only", async () => {
  const { controller, backend } = await started();
  const calls = backend.requests.length;
  const picked = pickSuggestion(controller.state, 2);
  assert.equal(picked.state.visible[2]?.candidate_id, "tc_3");
  assert.equal(backend.requests.length, calls);
  const aged = { ...controller.state, reserve: controller.state.reserve.map((item) => ({ ...item, fetched_at: 0 })) };
  const expired = expireReserve(aged, RESERVE_TTL_MS + 1);
  assert.equal(expired.reserve.length, 0);
  assert.deepEqual(visibleIds(expired), visibleIds(aged));
});

test("no duplicates across visible, reserve, the clicked one and recently shown", () => {
  let state = mergeSuggestions(emptySuggestions(), [candidate(0), candidate(1), candidate(0), { ...candidate(2), candidate_id: "tc_twin", question: "wie entstehen Polarlichter" }, candidate(2)], 1);
  assert.deepEqual(visibleIds(state), ["tc_0", "tc_1", "tc_2"]);
  const clicked = pickSuggestion(state, 0);
  state = mergeSuggestions(clicked.state, [candidate(0), candidate(1), candidate(3)], 2);
  const ids = [...state.visible, ...state.reserve].map((item) => item?.candidate_id);
  assert.deepEqual(ids, ["tc_3", "tc_1", "tc_2"]);
  assert.ok(refillRequest(state)!.exclude.includes("tc_0"));
});

test("persistence round-trips and rejects foreign data", () => {
  const state = mergeSuggestions(emptySuggestions(), [candidate(0), candidate(1), candidate(2), candidate(3)], 5);
  assert.deepEqual(parseSuggestions(serializeSuggestions(state)).visible, state.visible);
  assert.deepEqual(visibleIds(parseSuggestions("{broken")), [null, null, null]);
  assert.deepEqual(visibleIds(parseSuggestions(JSON.stringify({ version: 99, visible: [candidate(0)] }))), [null, null, null]);
});

test("discovery failure shows a clear note and never delays or blocks generation", async () => {
  const failing = await started(fakeBackend({ status: "unavailable" }));
  assert.equal(failing.controller.state.status, "unavailable");
  assert.equal(failing.controller.state.message, DISCOVERY_UNAVAILABLE);
  const offline = await started(fakeBackend({ fail: true }));
  assert.equal(offline.controller.state.status, "unavailable");
  // Existing chips survive a failed refill.
  const kept = await started();
  const shown = visibleIds(kept.controller.state);
  kept.controller.pick(0);
  assert.deepEqual(visibleIds(kept.controller.state).slice(1), shown.slice(1));
  // Generate never awaits topic discovery.
  const generate = home.slice(home.indexOf("async function generate()"), home.indexOf("async function removeFromQueue"));
  assert.doesNotMatch(generate, /loadTopicSuggestions|refill|suggestionsRef/);
  assert.match(home, /\{state\.message\}/);
});

test("manual textarea flow is unchanged; a chip question keeps its provenance", () => {
  assert.match(home, /placeholder="What should ClipForge create\?"/);
  assert.match(home, /await startGeneration\(prompt, options\)/);
  assert.match(home, /submitQuestionsInOrder\(questions, \(question\) => startGeneration\(question, options\)\)/);
  assert.match(home, /topic \? await startGeneration\(prompt, options, topic\) : await startGeneration\(prompt, options\)/);
  assert.equal(topicGenerationSource(null, "Warum ist der Himmel blau?"), undefined);
  const chip = { candidate_id: "tc_9", question: "Wie entsteht ein Gewitter?", rationale: "", confidence: "low", fetched_at: 0 };
  assert.deepEqual(topicGenerationSource(chip, "Wie entsteht ein Gewitter?"), { topic_source: "topic_intelligence", topic_candidate_id: "tc_9" });
  assert.equal(topicGenerationSource(chip, "   "), undefined);
  assert.match(home, /if \(!event\.target\.value\.trim\(\)\) setChosenTopic\(null\)/);
});

test("the separate Generate Next Video proposal UI is removed", () => {
  for (const text of ["Generate Next Video", "Try another", "Edit topic", "Generate video", "TopicProposal", "proposeNextTopic", "tryAnotherTopic"]) {
    assert.ok(!home.includes(text), text);
    assert.ok(!api.includes(text), text);
  }
  assert.match(api, /"\/topic-intelligence\/suggestions"/);
});
