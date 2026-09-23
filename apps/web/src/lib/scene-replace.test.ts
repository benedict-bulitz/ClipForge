import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import ts from "typescript";

test("scene alternatives and apply use the current scene and revision", async () => {
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  const code = ts.transpileModule(readFileSync(new URL("./api.ts", import.meta.url), "utf8"), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } }).outputText;
  const api: Record<string, (...args: unknown[]) => Promise<unknown>> = {};
  new Function("exports", "process", "fetch", code)(api, process, async (url: string, init?: RequestInit) => {
    calls.push({ url, init });
    return { ok: true, json: async () => ({}) };
  });
  await api.getSceneMediaCandidates("project", 2);
  assert.ok(calls[0].url.endsWith("/projects/project/scenes/2/media-candidates"));
  assert.equal(calls[0].init?.method, undefined);
  await api.applySceneMediaCandidate("project", 2, "set:0", 7);
  assert.ok(calls[1].url.endsWith("/media-candidates/apply"));
  assert.deepEqual(JSON.parse(String(calls[1].init?.body)), { token: "set:0", base_revision: 7 });
});
