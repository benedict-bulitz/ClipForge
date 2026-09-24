import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const workspace = readFileSync(new URL("../components/project-workspace.tsx", import.meta.url), "utf8");
const page = readFileSync(new URL("../app/page.tsx", import.meta.url), "utf8");

test("primary inputs and assistant composer have accessible names", () => {
  assert.match(page, /aria-label="Describe the video to create"/);
  assert.match(workspace, /aria-label="Project assistant message"/);
  assert.match(page, /aria-label="Type LÖSCHEN to confirm deletion"/);
});

test("music and cover controls expose state and keyboard-reachable titles", () => {
  assert.match(workspace, /aria-pressed=\{mode === "ai_matched"\}/);
  assert.match(workspace, /aria-pressed=\{thumbnails\.selected_variant_id === variant\.id\}/);
  assert.match(workspace, /tabIndex=\{0\} aria-label=\{`Music track:/);
  assert.match(workspace, /aria-label=\{`Track preview:/);
});

test("project detail tabs expose their selection semantics", () => {
  assert.match(workspace, /role="tablist"/);
  assert.match(workspace, /role="tab" aria-selected=\{tab === item\}/);
});
