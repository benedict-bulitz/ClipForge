import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { splitQuestions, submitQuestionsInOrder } from "./multi-question.ts";

const home = readFileSync(new URL("../app/page.tsx", import.meta.url), "utf8");

test("one question stays one question with its question mark", () => {
  assert.deepEqual(splitQuestions("  What is the tallest mountain?  "), ["What is the tallest mountain?"]);
});

test("question marks split several questions on one line", () => {
  const expected = ["What is the tallest mountain?", "What is the deepest ocean?", "Which country has the most pyramids?"];
  assert.deepEqual(splitQuestions(expected.join(" ")), expected);
});

test("line wrapping stays in one question while blank paragraphs split", () => {
  assert.deepEqual(splitQuestions("Why are there more pyramids in Sudan\nthan in Egypt and when were\nthey built?"), ["Why are there more pyramids in Sudan than in Egypt and when were they built?"]);
  assert.deepEqual(splitQuestions("Why are there more pyramids in Sudan\nthan in Egypt?\n\nWhat is larger: the number of trees\non Earth or the number of stars in\nthe Milky Way?"), [
    "Why are there more pyramids in Sudan than in Egypt?",
    "What is larger: the number of trees on Earth or the number of stars in the Milky Way?",
  ]);
});

test("numbered and bullet entries split without deduplicating", () => {
  assert.deepEqual(splitQuestions("1. What is the tallest mountain?\n2. What is the deepest ocean?\n3. Which country has the most pyramids?"), [
    "What is the tallest mountain?", "What is the deepest ocean?", "Which country has the most pyramids?",
  ]);
  assert.deepEqual(splitQuestions("- Why?\n- Why?"), ["Why?", "Why?"]);
});

test("blank lines and whitespace create no jobs; repeated questions remain separate", () => {
  assert.deepEqual(splitQuestions("\n  \n???  \n"), []);
  assert.deepEqual(splitQuestions("  Why does it rain? \n\n Why does it rain?  "), ["Why does it rain?", "Why does it rain?"]);
});

test("sequential creation preserves existing active and queued jobs, FIFO, and unique IDs", async () => {
  const queue = [
    { id: "old-active", project_id: "project-active", prompt: "Existing active" },
    { id: "old-queued", project_id: "project-queued", prompt: "Existing queued" },
  ];
  const questions = ["First question?", "Second question?", "Third question?"];
  const result = await submitQuestionsInOrder(questions, async (question) => {
    const index = queue.length;
    const created = { id: `job-${index}`, project_id: `project-${index}`, prompt: question };
    queue.push(created);
    return created;
  });
  assert.equal(result.failed, null);
  assert.deepEqual(queue.map((job) => job.prompt), ["Existing active", "Existing queued", ...questions]);
  assert.equal(new Set(queue.map((job) => job.project_id)).size, 5);
  assert.deepEqual(result.created.map((job) => job.prompt), questions);
});

test("identical questions still create two independent projects", async () => {
  let next = 0;
  const result = await submitQuestionsInOrder(splitQuestions("Why? Why?"), async () => ({ id: `job-${++next}`, project_id: `project-${next}` }));
  assert.equal(result.created.length, 2);
  assert.notEqual(result.created[0].project_id, result.created[1].project_id);
});

test("later failure reports its question and keeps earlier creations", async () => {
  let next = 0;
  const result = await submitQuestionsInOrder(["First?", "Second?", "Third?"], async () => {
    next += 1;
    if (next === 2) throw new Error("Provider unavailable");
    return { id: `job-${next}`, project_id: `project-${next}` };
  });
  assert.deepEqual(result.created.map((job) => job.project_id), ["project-1"]);
  assert.equal(result.failed?.index, 1);
  assert.equal(result.failed?.question, "Second?");
  assert.equal(next, 2);
});

test("toggle is off by default and single mode keeps its direct submission", () => {
  assert.match(home, /useState\(false\).*multipleQuestions|\[multipleQuestions, setMultipleQuestions\] = useState\(false\)/);
  assert.match(home, /Multiple questions/);
  assert.match(home, /if \(!multipleQuestions\)/);
  assert.match(home, /startGeneration\(prompt, options\)/);
  assert.match(home, /detectedQuestions\.length/);
});
