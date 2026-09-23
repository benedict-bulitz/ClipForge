import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const projectPage = readFileSync(new URL("../components/project-page.tsx", import.meta.url), "utf8");
const workspace = readFileSync(new URL("../components/project-workspace.tsx", import.meta.url), "utf8");

test("project route hydrates from its project ID instead of navigation memory", () => {
  assert.match(projectPage, /getProject\(projectId\)/);
  assert.match(projectPage, /\}, \[projectId\]\);/);
  assert.match(projectPage, /<ProjectWorkspace project=\{project\}/);
  assert.doesNotMatch(projectPage, /startGeneration|renderProject/);
});

test("workspace uses persisted render, audio, and scene state and handles a missing video", () => {
  assert.match(workspace, /mediaUrl\(state\.render\.url\)/);
  assert.match(workspace, /state\.music\.volume/);
  assert.match(workspace, /state\.voice\.volume/);
  assert.match(workspace, /scene\.media\?\.cache_path/);
  assert.match(workspace, /onError=\{\(\) => setVideoUnavailable\(true\)\}/);
  assert.match(workspace, /Die gespeicherte Videodatei ist nicht verfügbar/);
  assert.match(workspace, /Video erneut rendern/);
});
