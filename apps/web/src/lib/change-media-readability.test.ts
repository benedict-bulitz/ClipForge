import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const workspace = readFileSync(new URL("../components/project-workspace.tsx", import.meta.url), "utf8");
const css = readFileSync(new URL("../app/globals.css", import.meta.url), "utf8");

// The Change Media panel: from the candidate panel through the AI image option.
const panel = workspace.slice(workspace.indexOf("function GenerationStatus"), workspace.indexOf("function SourcesView"));
const mediaRules = css.slice(css.indexOf("/* Change Media panel"), css.indexOf(".media-selected-badge"));

test("change media uses theme-aware surfaces, never a hardcoded light panel", () => {
  // The real bug: a light panel in dark mode with inherited near-white text.
  assert.doesNotMatch(panel, /bg-\[#fffaf6\]|bg-white\/60|bg-white\b/);
  assert.match(panel, /className="media-panel /);
  assert.match(panel, /media-box mt-3/);
  // Explanatory copy has an explicit readable surface, not inherited colour.
  assert.match(panel, /className="media-note[^"]*">No suitable real media found/);
});

test("every change media action has an explicit, readable style", () => {
  assert.match(panel, /className="media-action[^"]*"><RefreshCw className="size-3" \/>Retry real media search/);
  assert.match(panel, /className="media-action[^"]*">Keep current media/);
  assert.match(panel, /className="media-primary[^"]*">Apply/);
  assert.match(panel, /className="media-strong [^"]*">\s*\{state\.status === "generating"/);
  assert.match(panel, /className="media-action [^"]*">\s*\{editing \? "Use automatic prompt" : "Edit prompt"\}/);
  // Disabled controls stay readable: no fading primary copy out with opacity.
  assert.doesNotMatch(panel, /disabled:opacity-/);
  assert.match(mediaRules, /\.media-action:disabled, \.media-primary:disabled, \.media-strong:disabled \{[^}]*color: var\(--muted-foreground\)/);
  assert.doesNotMatch(mediaRules, /opacity:/);
});

test("statuses and selection are distinguishable and token based", () => {
  assert.match(panel, /media-success text-xs font-semibold/);
  assert.match(panel, /state\.status === "unchanged" \? "media-warning" : "media-alert"/);
  assert.match(panel, /aria-pressed=\{candidateSelection === candidate\.token\}/);
  assert.match(panel, /media-selected-badge[^]*Selected/);
  // Only design tokens inside the Change Media rules; no one-off hex colours.
  assert.doesNotMatch(mediaRules, /#[0-9a-f]{3,8}\b/i);
  // Filled accent controls get a readable text colour in both themes.
  assert.match(css, /:root \{[^}]*--accent-solid: color-mix\(in srgb, var\(--accent\) 80%, #000\);[^}]*--on-accent-solid: #ffffff;/);
  assert.match(css, /html\.dark \{[^}]*--accent-solid: var\(--accent\);[^}]*--on-accent-solid: var\(--background\);/);
});
