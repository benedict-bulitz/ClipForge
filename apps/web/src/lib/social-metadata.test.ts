import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const api = readFileSync(new URL("./api.ts", import.meta.url), "utf8");
const workspace = readFileSync(new URL("../components/project-workspace.tsx", import.meta.url), "utf8");

test("social metadata has independent platform UI, editing, copying, and explicit generation", () => {
  assert.match(api, /social-metadata/);
  assert.match(workspace, /TikTok/);
  assert.match(workspace, /Instagram/);
  assert.match(workspace, /YouTube/);
  assert.match(workspace, /navigator\.clipboard\.writeText/);
  assert.match(workspace, /Kopiert/);
  assert.match(workspace, /Neu generieren/);
  assert.match(workspace, /Metadaten speichern/);
  assert.match(workspace, /aria-label=\{`\$\{platform\} title`\}/);
  assert.match(workspace, /aria-label=\{`\$\{platform\} description`\}/);
  assert.match(workspace, /aria-label=\{`\$\{platform\} hashtags`\}/);
  assert.match(workspace, /Copy all/);
  assert.match(workspace, /<SocialMetadata key=/);
  assert.ok(workspace.indexOf("<SocialMetadata project=") < workspace.indexOf('aria-label="Project detail tabs"'));
  assert.doesNotMatch(workspace, /\.slice\(0, 5\)/);
});
