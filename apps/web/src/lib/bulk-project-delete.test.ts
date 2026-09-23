import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const api = readFileSync(new URL("./api.ts", import.meta.url), "utf8");
const home = readFileSync(new URL("../app/page.tsx", import.meta.url), "utf8");

test("bulk deletion requests a read-only plan before calling the DELETE collection endpoint", () => {
  assert.match(api, /getBulkProjectDeletePlan/);
  assert.match(api, /\/projects\/delete-plan/);
  assert.match(api, /deleteAllProjects/);
  assert.match(api, /request<BulkProjectDeleteResult>\("\/projects", \{ method: "DELETE" \}\)/);
  assert.match(home, /await getBulkProjectDeletePlan\(\)/);
  assert.match(home, /await deleteAllProjects\(\)/);
});

test("bulk deletion requires a German typed confirmation and clears project state", () => {
  assert.match(home, /Alle Projekte wirklich löschen\?/);
  assert.match(home, /LÖSCHEN/);
  assert.match(home, /bulkDeletePhrase !== "LÖSCHEN"/);
  assert.match(home, /setRecent\(\[\]\)/);
  assert.match(home, /await refresh\(\)/);
  assert.match(home, /setQueue\(\[\]\)/);
  assert.match(home, /setBulkDeleteOpen\(false\)/);
  assert.doesNotMatch(home, /window\.confirm/);
});
