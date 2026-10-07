import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { deletableProjectCount, visibleProjectHistory } from "./queue-overview.ts";
import type { GenerationJob, ProjectOverview } from "./types.ts";

const home = readFileSync(new URL("../app/page.tsx", import.meta.url), "utf8");
const section = home.slice(home.indexOf('aria-labelledby="recent-projects-title"'), home.indexOf("{bulkDeleteOpen && bulkDeletePlan"));

const overview = (id: string, title: string, status: string, revision: number | null): ProjectOverview => ({ id, title, status, current_revision: revision, created_at: "", updated_at: "" });
const running = { id: "job-r", project_id: "running-1", status: "running" } as unknown as GenerationJob;

test("Recent Projects are normal page content: visible without clicking, no accordion", () => {
  assert.ok(section.length > 0);
  assert.match(section, /<h2 id="recent-projects-title"/);
  assert.doesNotMatch(home, /recentOpen|setRecentOpen|recent-projects-panel/);
  assert.doesNotMatch(section, /aria-expanded|ChevronDown/);
  // rendered whenever there is history - not gated by any toggle state
  assert.match(section, /\{history\.length > 0 \? \(\n\s+<div id="recent-projects-list"/);
  assert.doesNotMatch(section, /max-h-\[38rem\]|overflow-y-auto/); // the page scrolls, not a nested box
});

test("the count is the number of rendered projects", () => {
  assert.match(section, /Recent Projects\{history\.length > 0 \? ` \(\$\{history\.length\}\)` : ""\}/);
  assert.match(section, /\{history\.map\(\(project\) => \(/);
  // active generations are shown in the queue, not in the history nor its count
  const history = visibleProjectHistory([overview("a", "Why cats purr", "rendered", 1), overview("running-1", "Why?", "running", null)], [running]);
  assert.deepEqual(history.map((item) => item.id), ["a"]);
});

test("empty state after loading: subtle German copy, no card, no CTA", () => {
  assert.match(section, /projectsLoaded \? \(/);
  assert.match(section, /Noch keine Projekte/);
  assert.match(section, /Deine erstellten Videos erscheinen hier\./);
  const empty = section.slice(section.indexOf("Noch keine Projekte") - 200, section.indexOf("Deine erstellten Videos"));
  assert.doesNotMatch(empty, /cf-surface|workspace-card|<Button|<Link/);
});

test("meaningful failed projects stay in the history (the API drops only technical placeholders)", () => {
  const failed = overview("f", "Warum bin ich nach einem Mittagsschlaf manchmal müde?", "failed", null);
  const history = visibleProjectHistory([failed], []);
  assert.deepEqual(history, [failed]);
  // the page does not second-guess titles: the API rule is the one authority
  assert.doesNotMatch(home, /Untitled project/);
});

test("delete-all is shown whenever deletable history exists, including early-failed requests", () => {
  assert.equal(deletableProjectCount([]), 0);
  // an early-failed request without a project row is history that bulk delete clears too
  assert.equal(deletableProjectCount([overview("f", "Warum …", "failed", null)]), 1);
  assert.equal(deletableProjectCount([overview("f", "Warum …", "failed", null), overview("a", "Why cats purr", "rendered", 2)]), 2);
  // active generations are never part of a bulk delete
  assert.equal(deletableProjectCount([overview("r", "Running", "running", null), overview("q", "Queued", "queued", null)]), 0);
  assert.match(home, /const deletableProjects = deletableProjectCount\(history\);/);
  assert.match(section, /\{deletableProjects > 0 && \(\n\s+<button type="button" onClick=\{\(\) => void openBulkDelete\(\)\}/);
});

test("delete-all is a compact secondary danger action with a clear focus state", () => {
  const button = section.slice(section.indexOf("<button type=\"button\" onClick={() => void openBulkDelete()}"), section.indexOf("Alle Projekte löschen") + 30);
  assert.match(button, /<Trash2 className="size-3\.5" \/> Alle Projekte löschen/);
  assert.match(button, /text-\[11px\]/); // small, not a primary button
  assert.match(section, /<div className="mb-3 flex min-h-8 items-center justify-between gap-2 px-1 text-\[11px\]">/); // inherited size
  assert.match(button, /text-\[var\(--muted-foreground\)\]/); // quiet until intended
  assert.match(button, /hover:text-red-700/);
  assert.match(button, /focus-visible:ring-2 focus-visible:ring-red-500\/30/);
  assert.doesNotMatch(button, /<Button|variant="accent"|bg-\[#e95528\]/);
});

test("bulk deletion keeps its typed confirmation and tells what stays in Videos", () => {
  assert.match(home, /Alle Projekte wirklich löschen\?/);
  assert.match(home, /bulkDeletePhrase !== "LÖSCHEN"/);
  assert.match(home, /projects_keeping_learning_record/);
  assert.match(home, /in <SectionLink href="\/videos" className="underline">Videos<\/SectionLink> erhalten/);
  assert.match(home, /await deleteAllProjects\(\);[\s\S]*?await refresh\(\);/);
});

test("the confirmation counts early-failed requests and opens even without project rows", () => {
  assert.match(home, /setBulkDeleteOpen\(plan\.project_count \+ \(plan\.history_entry_count \?\? 0\) > 0\);/);
  assert.match(home, /\$\{history\} fehlgeschlagene \$\{history === 1 \? "Anfrage" : "Anfragen"\}/);
  assert.match(home, /\{bulkDeleteSummary\(bulkDeletePlan\)\}/);
});

test("the wording never implies that YouTube videos or their learning data are deleted", () => {
  const dialog = home.slice(home.indexOf('id="bulk-delete-title"'), home.indexOf("Zum Bestätigen"));
  assert.match(dialog, /lokal gerenderte Videodateien/);
  assert.doesNotMatch(dialog, /projektlokalen Videos|YouTube-Videos werden gelöscht|Lerndaten werden gelöscht/);
  assert.match(dialog, /bleib(t|en)"\} in <SectionLink href="\/videos"/);
  assert.match(dialog, /YouTube-Videos werden nie gelöscht\./);
});

test("after a successful bulk delete the page renders from an emptied history", () => {
  const success = home.slice(home.indexOf("const result = await deleteAllProjects();"), home.indexOf("setBulkDeleting(false);"));
  // local state is cleared, then the API's (now empty) history is loaded
  assert.match(success, /setRecent\(\[\]\);[\s\S]*setQueue\(\[\]\);[\s\S]*await refresh\(\);/);
  const history = visibleProjectHistory([], []);
  assert.equal(history.length, 0);
  assert.equal(deletableProjectCount(history), 0); // the delete-all action disappears
  // no count badge at all for zero entries, and the empty state takes its place
  assert.match(section, /Recent Projects\{history\.length > 0 \? ` \(\$\{history\.length\}\)` : ""\}/);
  assert.match(section, /\) : projectsLoaded \? \(\n\s+<div className="px-1 py-2">\n\s+<p className="text-sm font-semibold">Noch keine Projekte<\/p>/);
});
