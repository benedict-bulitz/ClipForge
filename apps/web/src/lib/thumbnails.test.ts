import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";

const workspace = readFileSync(new URL("../components/project-workspace.tsx", import.meta.url), "utf8");
const api = readFileSync(new URL("./api.ts", import.meta.url), "utf8");

test("finished workspace exposes persisted cover variants without video rerender", () => {
  assert.match(workspace, /aria-label=\"Cover thumbnails\"/);
  assert.match(workspace, /generateProjectThumbnails\(/);
  assert.match(workspace, /selectProjectThumbnail\(/);
  assert.match(workspace, /thumbnails\.selected_variant_id/);
  assert.doesNotMatch(workspace, /renderProject\(project\.id, project\.current_revision\).*thumbnail/i);
  assert.match(api, /\/projects\/\$\{projectId\}\/thumbnails\/generate/);
  assert.match(api, /\/projects\/\$\{projectId\}\/thumbnails/);
});
