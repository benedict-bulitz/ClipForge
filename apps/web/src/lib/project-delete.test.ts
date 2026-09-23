import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const api = readFileSync(new URL("./api.ts", import.meta.url), "utf8");
const workspace = readFileSync(new URL("../components/project-workspace.tsx", import.meta.url), "utf8");

test("project deletion uses the DELETE API and an explicit German confirmation dialog", () => {
  assert.match(api, /deleteProject\(projectId: string\)/);
  assert.match(api, /method: "DELETE"/);
  assert.match(workspace, /Projekt wirklich löschen\?/);
  assert.match(workspace, /Das Projekt und seine zugehörigen Dateien werden dauerhaft gelöscht\./);
  assert.match(workspace, />Abbrechen</);
  assert.match(workspace, /Projekt löschen/);
  assert.match(workspace, /deleteConfirmationOpen/);
  assert.doesNotMatch(workspace, /window\.confirm/);
});

test("confirmed deletion clears active UI state and returns to the project overview", () => {
  assert.match(workspace, /await deleteProject\(project\.id\)/);
  assert.match(workspace, /setMessages\(\[\]\)/);
  assert.match(workspace, /router\.replace\("\/"\)/);
});

test("cancelling the dialog only closes it", () => {
  assert.match(workspace, /onClick=\{\(\) => setDeleteConfirmationOpen\(false\)\}/);
});
