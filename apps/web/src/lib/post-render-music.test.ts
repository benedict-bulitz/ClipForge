import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const workspace = readFileSync(new URL("../components/project-workspace.tsx", import.meta.url), "utf8");
const api = readFileSync(new URL("./api.ts", import.meta.url), "utf8");

test("post-render music controls expose previews, both selection modes, and persistent selection APIs", () => {
  assert.match(workspace, /Background music/);
  assert.match(workspace, /AI Matched/);
  assert.match(workspace, /All Music/);
  assert.match(workspace, /<audio/);
  assert.match(workspace, /Change music/);
  assert.match(workspace, /Music volume/);
  assert.match(api, /listProjectMusicTracks/);
  assert.match(api, /updateProjectMusicSelection/);
  assert.match(api, /\/projects\/\$\{projectId\}\/music/);
  assert.match(workspace, /exportProject\(project\.id, project\.current_revision\)/);
  assert.doesNotMatch(workspace, /renderProject\(project\.id, project\.current_revision\).*MusicControls/);
});
