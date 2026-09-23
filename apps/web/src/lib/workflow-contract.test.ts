import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const workspace = readFileSync(new URL("../components/project-workspace.tsx", import.meta.url), "utf8");
const voiceControls = readFileSync(new URL("../components/voice-controls.tsx", import.meta.url), "utf8");
const api = readFileSync(new URL("./api.ts", import.meta.url), "utf8");

test("undo and redo use durable backend capabilities and avoid text-editing controls", () => {
  assert.match(api, /\/projects\/\$\{projectId\}\/redo/);
  assert.match(workspace, /project\.can_undo/);
  assert.match(workspace, /project\.can_redo/);
  assert.match(workspace, /target\.isContentEditable/);
  assert.match(workspace, /event\.shiftKey/);
  assert.match(workspace, /event\.key\.toLowerCase\(\) === "y"/);
  assert.match(workspace, /aria-label="Undo latest edit"/);
  assert.match(workspace, /aria-label="Redo latest undone edit"/);
});

test("scene media chooser discovers candidates and applies through a revision", () => {
  assert.match(api, /getSceneMediaCandidates/);
  assert.match(api, /applySceneMediaCandidate/);
  assert.match(workspace, /getSceneMediaCandidates/);
  assert.match(workspace, /applySceneMediaCandidate/);
  assert.match(workspace, /mediaBusy/);
  assert.match(workspace, /Loading…/);
  assert.match(workspace, /Alternatives/);
  assert.match(workspace, />Apply</);
  assert.match(workspace, /candidate.preview_url/);
  assert.match(workspace, /candidate.provider/);
});

test("voice previews debounce, cancel stale requests, and keep autoplay rejection usable", () => {
  assert.match(voiceControls, /new AbortController\(\)/);
  assert.match(voiceControls, /requestRef\.current !== requestId/);
  assert.match(voiceControls, /previous\.speed !== speed \? 800 : 400/);
  assert.match(voiceControls, /resolvedLanguage: language/);
  assert.match(voiceControls, /promptRef\.current/);
  assert.match(voiceControls, /controller\.abort\(\)/);
  assert.match(voiceControls, /autoplayBlocked/);
  assert.doesNotMatch(voiceControls, />Update preview</);
  assert.doesNotMatch(voiceControls, />Preview</);
});

test("AI Review exposes actual findings and a safe empty-contract warning", () => {
  assert.match(workspace, /review\?\.items/);
  assert.match(workspace, /findings\.map/);
  assert.match(workspace, /item\.check/);
  assert.match(workspace, /item\.severity/);
  assert.match(workspace, /aria-expanded=\{expanded\}/);
  assert.match(workspace, /supplied no visible findings/);
  assert.doesNotMatch(workspace, /chain-of-thought/i);
});

test("alignment readiness distinguishes precision degradation from hard failure", () => {
  assert.match(workspace, /Precise word-level caption timing is available/);
  assert.match(workspace, /less precise phrase timing/);
  assert.match(workspace, /Videos can still be generated/);
  assert.match(workspace, /needs setup before its part of production can run/);
});

test("scene cards preview cached media while preserving the attribution fallback", () => {
  assert.match(workspace, /scene\.media\?\.cache_path/);
  assert.match(workspace, /mediaUrl\(`\/media\/\$\{scene\.media\.cache_path\}`\)/);
  assert.match(workspace, /<video/);
  assert.match(workspace, /<img/);
  assert.match(workspace, /Scene \$\{index \+ 1\} video preview/);
  assert.match(workspace, /scene\.media\.source_url/);
});
