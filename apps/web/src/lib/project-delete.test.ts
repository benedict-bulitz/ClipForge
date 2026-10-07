import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const api = readFileSync(new URL("./api.ts", import.meta.url), "utf8");
const workspace = readFileSync(new URL("../components/project-workspace.tsx", import.meta.url), "utf8");

test("project deletion uses the DELETE API and an upload-aware explicit confirmation dialog", () => {
  assert.match(api, /deleteProject\(projectId: string\)/);
  assert.match(api, /method: "DELETE"/);
  assert.match(api, /\/delete-plan/);
  assert.match(api, /confirm_unverified=true/);
  assert.match(workspace, /getProjectDeletePlan\(project\.id\)/);
  assert.match(workspace, /deleteDialogCopy\(deletePlan\)/);
  assert.match(workspace, /Local storage to be freed/);
  assert.match(workspace, /Learning data retained/);
  assert.match(workspace, />Cancel</);
  assert.match(workspace, /deleteConfirmationOpen/);
  assert.doesNotMatch(workspace, /window\.confirm/);
});

test("confirmed deletion clears active UI state and returns to the project overview", () => {
  assert.match(workspace, /await deleteProject\(project\.id\)/);
  assert.match(workspace, /setMessages\(\[\]\)/);
  assert.match(workspace, /window\.location\.replace\("\/"\)/);
});

test("cancelling the dialog only closes it", () => {
  assert.match(workspace, /onClick=\{\(\) => setDeleteConfirmationOpen\(false\)\}/);
});
