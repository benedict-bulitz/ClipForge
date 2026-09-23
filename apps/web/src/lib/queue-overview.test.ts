import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { activeQueueJobs, visibleProjectHistory } from "./queue-overview.ts";
import type { GenerationJob, ProjectOverview } from "./types.ts";

const home = readFileSync(new URL("../app/page.tsx", import.meta.url), "utf8");
const settings = readFileSync(new URL("../components/advanced-options.tsx", import.meta.url), "utf8");

function job(id: string, prompt: string, status: GenerationJob["status"], position: number | null): GenerationJob {
  return { id: `job-${id}`, project_id: id, prompt, status, queue_position: position, progress: 0.73, base_revision: null, current_stage: "preparing", stage_label: "Preparing", completed_units: null, total_units: null, started_at: null, updated_at: "2026-09-23", completed_at: null, elapsed_seconds: 0, estimated_remaining_seconds: null, failure_category: null, failure_message: null };
}

test("A, B, C, D retain stable project identities through polling and completion", () => {
  const a = job("A", "Warum wird uns schwarz vor Augen?", "running", null);
  const b = job("B", "Warum bekommen wir Schluckauf?", "queued", 1);
  const c = job("C", "Warum können Flugzeuge fliegen?", "queued", 2);
  const d = job("D", "Warum knackt Holz im Feuer?", "queued", 3);
  const first = activeQueueJobs([d, c, a, b]);
  assert.deepEqual(first.map((item) => [item.project_id, item.prompt]), [["A", a.prompt], ["B", b.prompt], ["C", c.prompt], ["D", d.prompt]]);
  assert.equal(first.length, 4);
  const refreshed = activeQueueJobs([a, b, c, d]);
  assert.deepEqual(refreshed.map((item) => item.project_id), first.map((item) => item.project_id));
  const later = activeQueueJobs([job("A", a.prompt, "completed", null), job("B", b.prompt, "running", null), job("C", c.prompt, "queued", 1), job("D", d.prompt, "queued", 2)]);
  assert.deepEqual(later.map((item) => item.project_id), ["B", "C", "D"]);
  assert.equal(later.length, 3);
});

test("completed projects stay in full history and active rows are not duplicated", () => {
  const projects: ProjectOverview[] = [
    { id: "A", title: "A", status: "rendered", current_revision: 2, created_at: "2026-09-20", updated_at: "2026-09-23" },
    { id: "B", title: "B", status: "running", current_revision: null, created_at: "2026-09-21", updated_at: "2026-09-23" },
    ...Array.from({ length: 25 }, (_, index) => ({ id: `old-${index}`, title: `Old ${index}`, status: "rendered", current_revision: 2, created_at: "2026-09-01", updated_at: "2026-09-01" })),
  ];
  const visible = visibleProjectHistory(projects, [job("B", "B", "running", null)]);
  assert.equal(visible.length, 26);
  assert.equal(visible[0].id, "A");
  assert.ok(visible.some((project) => project.id === "old-24"));
});

test("removed queue jobs stay out of the active count while project history remains visible", () => {
  const active = job("A", "Active", "running", null);
  const removed = job("B", "Same question", "removed", null);
  const completed = job("C", "Completed", "completed", null);
  assert.deepEqual(activeQueueJobs([active, removed, completed]).map((item) => item.project_id), ["A"]);
  const projects: ProjectOverview[] = [
    { id: "B", title: "Same question", status: "draft", current_revision: null, created_at: "2026-09-20", updated_at: "2026-09-23" },
    { id: "C", title: "Completed", status: "rendered", current_revision: 1, created_at: "2026-09-20", updated_at: "2026-09-23" },
  ];
  assert.deepEqual(visibleProjectHistory(projects, [active, removed, completed]).map((project) => project.id), ["B", "C"]);
});

test("queue toggle, stable keys, and expandable complete history are wired", () => {
  assert.match(settings, /Advanced settings/);
  assert.match(settings, /queueToggle/);
  assert.match(home, /Video Queue/);
  assert.match(home, /\{activeJobs\.length\}/);
  assert.match(home, /key=\{item\.project_id\}/);
  assert.match(home, /running\.map/);
  assert.match(home, /waiting\.map/);
  assert.match(home, /Recent Projects/);
  assert.match(home, /setRecentOpen/);
  assert.match(home, /history\.map/);
  assert.match(home, /href=\{`\/projects\/\$\{project\.id\}`\}/);
  assert.doesNotMatch(home, /slice\(0, 4\)/);
  assert.match(home, /removeQueuedGenerationJob/);
  assert.match(home, /clearGenerationQueue/);
  assert.match(home, /Clear Queue/);
  assert.match(home, /onRemove\(item\.id\)/);
});
