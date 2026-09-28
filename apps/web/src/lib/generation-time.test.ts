import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { formatDuration, generationTimeLabel } from "./generation-poll.ts";
import type { GenerationJob } from "./types.ts";

const projectPage = readFileSync(new URL("../components/project-page.tsx", import.meta.url), "utf8");
const home = readFileSync(new URL("../app/page.tsx", import.meta.url), "utf8");

function job(overrides: Partial<GenerationJob>): GenerationJob {
  return {
    id: "job-1", project_id: "p-1", prompt: "Warum?", base_revision: null, status: "running", current_stage: "quality_review",
    stage_label: "Reviewing the final video", progress: 0.9, completed_units: null, total_units: null, started_at: null, updated_at: "",
    completed_at: null, elapsed_seconds: 0, estimated_remaining_seconds: null, failure_category: null, failure_message: null, queue_position: null, ...overrides,
  };
}

test("durations read as m:ss or h:mm:ss", () => {
  assert.equal(formatDuration(0), "0:00");
  assert.equal(formatDuration(493.4), "8:13");
  assert.equal(formatDuration(699), "11:39");
  assert.equal(formatDuration(3725), "1:02:05");
  assert.equal(formatDuration(Number.NaN), "0:00");
});

test("running jobs show elapsed and the backend's remaining estimate", () => {
  assert.equal(generationTimeLabel(job({ elapsed_seconds: 493, estimated_remaining_seconds: 206 })), "8:13 elapsed · ~3:26 remaining");
  // No (or no positive) estimate: elapsed only, never an invented number.
  assert.equal(generationTimeLabel(job({ elapsed_seconds: 12, estimated_remaining_seconds: null })), "0:12 elapsed");
  assert.equal(generationTimeLabel(job({ elapsed_seconds: 12, estimated_remaining_seconds: 0 })), "0:12 elapsed");
});

test("completed jobs show the total time; queued and failed jobs show none", () => {
  assert.equal(generationTimeLabel(job({ status: "completed", elapsed_seconds: 699, estimated_remaining_seconds: 0 })), "Completed in 11:39");
  assert.equal(generationTimeLabel(job({ status: "queued", elapsed_seconds: 30 })), null);
  assert.equal(generationTimeLabel(job({ status: "failed", elapsed_seconds: 30 })), null);
  assert.equal(generationTimeLabel(null), null);
});

test("generation views render the timing line and the current stage", () => {
  assert.match(projectPage, /generationTimeLabel\(job\)/);
  assert.match(projectPage, /job\.stage_label/);
  assert.match(home, /generationTimeLabel\(item\)/);
});
