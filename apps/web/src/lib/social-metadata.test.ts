import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const api = readFileSync(new URL("./api.ts", import.meta.url), "utf8");
const workspace = readFileSync(new URL("../components/project-workspace.tsx", import.meta.url), "utf8");
const projectPage = readFileSync(new URL("../components/project-page.tsx", import.meta.url), "utf8");

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
  assert.doesNotMatch(workspace, /copy\(drafts\[platform\]\.(title|description|hashtags)\)/);
  assert.match(workspace, /<SocialMetadata key=/);
  assert.ok(workspace.indexOf("<SocialMetadata project=") < workspace.indexOf('aria-label="Project detail tabs"'));
  assert.doesNotMatch(workspace, /\.slice\(0, 5\)/);
});

test("one project workspace mounts social metadata and thumbnails once with distinct keys", () => {
  assert.equal((workspace.match(/<SocialMetadata key=/g) ?? []).length, 1);
  assert.equal((workspace.match(/<ThumbnailControls key=/g) ?? []).length, 1);
  assert.match(workspace, /<SocialMetadata key=\{`social:\$\{project\.id\}:\$\{project\.current_revision\}`\}/);
  assert.match(workspace, /<ThumbnailControls key=\{`thumbnail:\$\{project\.id\}:\$\{project\.current_revision\}`\}/);
  assert.match(projectPage, /setProject\(value\)/);
  assert.doesNotMatch(projectPage, /setProject\(\(current\) => \[\.\.\.current/);
});
