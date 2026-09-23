import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import ts from "typescript";

test("audio controls save revision settings and reopen from server state", async () => {
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  const saved = { current_revision: 3, revision: { state: { voice: { volume: 0.6 }, music: { volume: 0.2, enabled: false, track: { id: "same-song" } } } } };
  // Load the real API module without depending on Next's runtime loader.
  const code = ts.transpileModule(readFileSync(new URL("./api.ts", import.meta.url), "utf8"), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } }).outputText;
  const exports: Record<string, (...args: unknown[]) => Promise<typeof saved>> = {};
  new Function("exports", "process", "fetch", code)(exports, process, async (url: string, init?: RequestInit) => {
    calls.push({ url, init });
    return { ok: true, json: async () => saved };
  });
  const updated = await exports.updateProjectAudio("project", 2, { voice_volume: 0.6, music_volume: 0.2, music_enabled: false });
  assert.equal(calls[0].init?.method, "PATCH");
  assert.deepEqual(JSON.parse(String(calls[0].init?.body)), { base_revision: 2, voice_volume: 0.6, music_volume: 0.2, music_enabled: false });
  const reopened = await exports.getProject("project");
  assert.deepEqual(reopened, updated);
  assert.equal(reopened.revision.state.music.track.id, "same-song");
});
