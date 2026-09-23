import assert from "node:assert/strict";
import test from "node:test";
import {
  DEFAULT_CREATE_OPTIONS,
  parseCreatePreferences,
  sanitizeCreatePreferences,
  serializeCreatePreferences,
} from "./creation-preferences.ts";

test("creation preferences omit prompts and secrets", () => {
  const serialized = serializeCreatePreferences({
    ...DEFAULT_CREATE_OPTIONS,
    prompt: "private project idea",
    api_key: "unit-secret-value",
    openai_api_key: "unit-secret-value",
  });
  assert.equal(serialized.includes("private project idea"), false);
  assert.equal(serialized.includes("unit-secret-value"), false);
});

test("corrupt and obsolete preferences safely use current defaults", () => {
  assert.deepEqual(parseCreatePreferences("not json"), DEFAULT_CREATE_OPTIONS);
  const migrated = sanitizeCreatePreferences({ language: "obsolete", aspect_ratio: "4:3", caption_style: "old-style", music_volume: 99 });
  assert.equal(migrated.language, "auto");
  assert.equal(migrated.aspect_ratio, "9:16");
  assert.equal(migrated.caption_style, "karaoke");
  assert.equal(migrated.music_volume, 0.14);
});

test("reset state is an independent default copy", () => {
  const first = sanitizeCreatePreferences(null);
  first.voice_speed = 1.2;
  const second = sanitizeCreatePreferences(null);
  assert.equal(second.voice_speed, 1);
  assert.equal(second.min_duration, null);
  assert.equal(second.max_duration, 60);
  assert.equal(second.pacing, "fast");
  assert.equal(second.music_enabled, true);
  assert.equal(second.music_mood, null);
});

test("valid existing pacing and duration preferences remain respected", () => {
  const restored = sanitizeCreatePreferences({
    version: 1,
    preferences: { pacing: "balanced", min_duration: 30, max_duration: 60 },
  });
  assert.equal(restored.pacing, "balanced");
  assert.equal(restored.min_duration, 30);
  assert.equal(restored.max_duration, 60);
  const invalid = sanitizeCreatePreferences({ min_duration: 90, max_duration: 30 });
  assert.equal(invalid.min_duration, null);
});
