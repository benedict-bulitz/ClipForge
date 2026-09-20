import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const home = readFileSync(new URL("../app/page.tsx", import.meta.url), "utf8");
const api = readFileSync(new URL("./api.ts", import.meta.url), "utf8");

test("Generate uses a trackable backend job instead of blocking create and render calls", () => {
  assert.match(api, /\/generation-jobs/);
  assert.match(home, /startGeneration\(prompt, options\)/);
  assert.match(home, /getGenerationJob\(activeJobId/);
  assert.doesNotMatch(home, /await createProject\(prompt/);
  assert.doesNotMatch(home, /await renderProject\(/);
});

test("progress card renders backend percent, work units, ETA, and safe failure", () => {
  assert.match(home, /job\.progress \* 100/);
  assert.match(home, /role="progressbar"/);
  assert.match(home, /job\.completed_units/);
  assert.match(home, /job\.estimated_remaining_seconds/);
  assert.match(home, /job\.failure_message/);
  assert.match(home, /Less than 10 sec remaining/);
});

test("polling is non-overlapping, stops at terminal state, and survives refresh", () => {
  assert.match(home, /window\.setTimeout\(poll, 750\)/);
  assert.doesNotMatch(home, /setInterval/);
  assert.match(home, /next\.status === "completed"/);
  assert.match(home, /next\.status === "failed"/);
  assert.match(home, /clipforge\.active-generation-job/);
  assert.match(home, /getActiveGenerationJob/);
});

test("rapid Generate requests are guarded without fake timer progress", () => {
  assert.match(home, /generationRequest\.current/);
  assert.match(home, /disabled=\{prompt\.trim\(\)\.length < 3 \|\| loading \|\| jobActive\}/);
  assert.doesNotMatch(home, /setProgress/);
});
