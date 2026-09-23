import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const home = readFileSync(new URL("../app/page.tsx", import.meta.url), "utf8");
const api = readFileSync(new URL("./api.ts", import.meta.url), "utf8");

test("Generate uses a trackable backend job instead of blocking create and render calls", () => {
  assert.match(api, /\/generation-jobs/);
  assert.match(home, /startGeneration\(prompt, options\)/);
  assert.match(api, /listGenerationJobs/);
  assert.match(home, /GenerationQueue/);
  assert.doesNotMatch(home, /await createProject\(prompt/);
  assert.doesNotMatch(home, /await renderProject\(/);
});

test("active queue row renders persisted progress and topic", () => {
  assert.match(home, /item\.progress \* 100/);
  assert.match(home, /role="progressbar"/);
  assert.match(home, /item\.prompt/);
  assert.match(home, /Wird erstellt/);
});

test("queue state refreshes without navigation or a full page reload", () => {
  assert.match(home, /window\.setTimeout\(\(\) => void poll\(\), 2500\)/);
  assert.match(home, /listGenerationJobs\(\)/);
  assert.match(home, /item\.queue_position/);
  assert.match(home, /In Warteschlange/);
  assert.doesNotMatch(home, /setQueue\(\(items\) => \[started/);
});

test("rapid Generate requests are guarded without fake timer progress", () => {
  assert.match(home, /generationRequest\.current/);
  assert.match(home, /disabled=\{prompt\.trim\(\)\.length < 3 \|\| loading\}/);
  assert.doesNotMatch(home, /disabled=\{jobActive\}/);
  assert.doesNotMatch(home, /setProgress/);
});
