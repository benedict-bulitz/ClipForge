import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { tripleHookSummary } from "./triple-hook.ts";

const workspace = readFileSync(new URL("../components/project-workspace.tsx", import.meta.url), "utf8");

test("triple hook summary exposes the selected opening compactly", () => {
  const summary = tripleHookSummary({
    version: 2,
    status: "connected",
    hook_id: "hook_a",
    verbal_hook: "Deine Finger schrumpeln nicht, weil sie Wasser aufsaugen.",
    on_screen_text_hook: "Dein Nervensystem steckt dahinter",
    selected_strategy: "counterintuitive_insight",
    supported_by_fact_ids: ["fact_01", "fact_02"],
    score: 88.4,
    reason_codes: ["researched_contrast", "specific", "clean_transition", "high_curiosity"],
    visual_hook: { framing: "extreme close-up", subject: "wet fingertips with deep wrinkles", action_state: "gripping a wet stone" },
    selection: { candidate_count: 4, eligible_count: 2, judge: { status: "connected" } },
  });
  assert.deepEqual(summary, {
    verbal: "Deine Finger schrumpeln nicht, weil sie Wasser aufsaugen.",
    onScreen: "Dein Nervensystem steckt dahinter",
    visual: "extreme close-up, wet fingertips with deep wrinkles, gripping a wet stone",
    strategy: "counterintuitive_insight",
    evidence: "fact_01, fact_02",
    meta: "score 88 · 2/4 candidates passed · researched contrast · specific · clean transition",
  });
});

test("omitted on-screen hook is shown as omitted, and old projects show nothing", () => {
  const summary = tripleHookSummary({ version: 2, verbal_hook: "Hook.", on_screen_text_hook: "", on_screen_omitted_reason: "on_screen_generic", status: "fallback", selected_strategy: "evidence_insight" });
  assert.equal(summary?.strategy, "evidence_insight");
  assert.equal(summary?.onScreen, "omitted (on screen generic)");
  assert.equal(summary?.meta, "fallback");
  assert.equal(tripleHookSummary({ verbal_hook: "Legacy V1 hook", on_screen_text_hook: "WHO HAS MORE?" }), null);
  assert.equal(tripleHookSummary(undefined), null);
});

test("creative direction renders the opening hook", () => {
  assert.match(workspace, /tripleHookSummary\(state\.script\.triple_hook\)/);
  assert.match(workspace, /aria-label="Opening hook"/);
});
