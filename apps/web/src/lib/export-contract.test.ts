import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const workspace = readFileSync(
  new URL("../components/project-workspace.tsx", import.meta.url),
  "utf8",
);
const api = readFileSync(new URL("./api.ts", import.meta.url), "utf8");

test("Export MP4 uses the project export action instead of a raw render link", () => {
  assert.match(workspace, /exportProject\(project\.id, project\.current_revision\)/);
  assert.doesNotMatch(workspace, /href=\{downloadUrl\}/);
  assert.doesNotMatch(workspace, /<a[^>]+download/);
  assert.match(api, /`\/projects\/\$\{projectId\}\/export`/);
  assert.match(api, /method: "POST"/);
});
