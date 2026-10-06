import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { ALERT_TONES, alertClass, alertRole, badgeClass, noticeTone, restrictionTone, textToneClass, toneClass } from "./alerts.ts";

const read = (path: string) => readFileSync(new URL(path, import.meta.url), "utf8");
const css = read("../app/globals.css");
const component = read("../components/ui/alert.tsx");

/** Every file migrated to the shared system: no raw status palette may come back. */
const MIGRATED = [
  "../components/social-publish-sheet.tsx",
  "../components/social-publications.tsx",
  "../components/publishing-integrations.tsx",
  "../components/upload-sheet.tsx",
  "../components/integrations-settings.tsx",
  "../components/youtube-publish-sheet.tsx",
  "../components/youtube-schedule-fields.tsx",
  "../components/youtube-publishing-schedule.tsx",
  "../components/youtube-panel.tsx",
  "../components/queue-overview.tsx",
  "../components/video-library.tsx",
  "../components/video-detail.tsx",
  "../components/channel-performance.tsx",
  "../components/project-workspace.tsx",
  "../components/project-page.tsx",
];

function tokens(block: string): Record<string, string> {
  return Object.fromEntries([...block.matchAll(/--([a-z-]+):\s*([^;]+);/g)].map((match) => [match[1], match[2].trim()]));
}

const LIGHT = tokens(css.slice(css.indexOf(":root {"), css.indexOf("}", css.indexOf(":root {"))));
const DARK = tokens(css.slice(css.indexOf("html.dark {"), css.indexOf("}", css.indexOf("html.dark {"))));

type RGB = [number, number, number];

function parse(color: string, under: RGB = [255, 255, 255]): RGB {
  const hex = color.match(/^#([0-9a-f]{6})$/i);
  if (hex) return [0, 2, 4].map((index) => parseInt(hex[1].slice(index, index + 2), 16)) as RGB;
  const rgba = color.match(/^rgba\((\d+),\s*(\d+),\s*(\d+),\s*([\d.]+)\)$/);
  assert.ok(rgba, `unsupported colour ${color}`);
  const alpha = Number(rgba![4]);
  return [1, 2, 3].map((index, channel) => Number(rgba![index]) * alpha + under[channel] * (1 - alpha)) as RGB;
}

function luminance([r, g, b]: RGB): number {
  const linear = (value: number) => {
    const channel = value / 255;
    return channel <= 0.03928 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * linear(r) + 0.7152 * linear(g) + 0.0722 * linear(b);
}

function contrast(a: RGB, b: RGB): number {
  const [high, low] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (high + 0.05) / (low + 0.05);
}

// ---------------------------------------------------------------------------
// The system itself
// ---------------------------------------------------------------------------

test("four semantic variants exist with one class vocabulary", () => {
  assert.deepEqual(ALERT_TONES, ["error", "warning", "success", "info"]);
  assert.equal(alertClass("warning"), "cf-alert cf-tone-warning");
  assert.equal(alertClass("error", "sm"), "cf-alert cf-alert-sm cf-tone-error");
  assert.equal(badgeClass("muted"), "cf-badge cf-tone-muted");
  assert.equal(textToneClass("success"), "cf-text-success");
  assert.equal(toneClass("info"), "cf-tone-info");
  for (const tone of ALERT_TONES) {
    assert.match(css, new RegExp(`\\.cf-tone-${tone} \\{ color: var\\(--${tone}-fg\\); background-color: var\\(--${tone}-bg\\); border-color: var\\(--${tone}-border\\); \\}`));
    assert.match(css, new RegExp(`\\.cf-text-${tone} \\{ color: var\\(--${tone}-fg\\); \\}`));
    assert.match(css, new RegExp(`\\.cf-tone-${tone} \\.cf-alert-icon \\{ color: var\\(--${tone}-icon\\); \\}`));
    for (const part of ["fg", "bg", "border", "icon"]) {
      assert.ok(LIGHT[`${tone}-${part}`], `light --${tone}-${part}`);
      assert.ok(DARK[`${tone}-${part}`], `dark --${tone}-${part}`);
    }
  }
  // Shape is shared by every tone: one border, radius, spacing and type scale.
  assert.match(css, /\.cf-alert \{ display: flex; align-items: flex-start; gap: \.5rem; border-width: 1px; border-style: solid; border-radius: \.75rem;/);
});

test("every variant is readable in light and dark mode (WCAG AA 4.5:1)", () => {
  const lightSurface: RGB = [255, 255, 255];
  const darkSurface = parse("#292a25");
  for (const tone of ALERT_TONES) {
    const light = contrast(parse(LIGHT[`${tone}-fg`]), parse(LIGHT[`${tone}-bg`], lightSurface));
    const dark = contrast(parse(DARK[`${tone}-fg`]), parse(DARK[`${tone}-bg`], darkSurface));
    assert.ok(light >= 4.5, `${tone} light contrast ${light.toFixed(2)}`);
    assert.ok(dark >= 4.5, `${tone} dark contrast ${dark.toFixed(2)}`);
  }
});

test("warning text is dark amber in light mode and light amber in dark mode - never red/orange", () => {
  const hue = (color: RGB) => {
    const [r, g, b] = color.map((value) => value / 255);
    const max = Math.max(r, g, b);
    const min = Math.min(r, g, b);
    if (max === min) return 0;
    const raw = max === r ? ((g - b) / (max - min)) % 6 : max === g ? (b - r) / (max - min) + 2 : (r - g) / (max - min) + 4;
    return (raw * 60 + 360) % 360;
  };
  for (const color of [parse(LIGHT["warning-fg"]), parse(DARK["warning-fg"])]) {
    const value = hue(color);
    assert.ok(value >= 35 && value <= 55, `warning hue ${value.toFixed(0)} must be amber, not red/orange`);
  }
  assert.ok(luminance(parse(LIGHT["warning-fg"])) < 0.1, "dark text in light mode");
  assert.ok(luminance(parse(DARK["warning-fg"])) > 0.5, "light text in dark mode");
});

test("the Alert component maps tone -> icon, role and classes", () => {
  assert.match(component, /const ICONS = \{ error: CircleAlert, warning: AlertTriangle, success: CheckCircle2, info: Info \}/);
  assert.match(component, /className=\{cn\(alertClass\(tone, size\), className\)\}/);
  assert.equal(alertRole("error"), "alert");
  assert.equal(alertRole("warning"), "status");
  assert.equal(alertRole("info"), "status");
  assert.equal(noticeTone("info"), "info");
  assert.equal(noticeTone("warn"), "warning");
  assert.equal(noticeTone("ok"), "success");
});

// ---------------------------------------------------------------------------
// Severity rules
// ---------------------------------------------------------------------------

test("the TikTok unaudited-app notice is a warning; blocking restrictions are errors", () => {
  const unaudited = { code: "unaudited_client", message: "Public Direct Post requires TikTok app approval. Until the app passes TikTok's audit, posts are private (Only me) and the TikTok account must be set to private.", blocks_publishing: false };
  assert.equal(restrictionTone(unaudited), "warning");
  assert.equal(restrictionTone({ ...unaudited, blocks_publishing: true }), "warning"); // always a limitation, never styled as a failure
  assert.equal(restrictionTone({ code: "token_expiring", blocks_publishing: false }), "warning");
  assert.equal(restrictionTone({ code: "insufficient_scope", blocks_publishing: true }), "error");
  assert.equal(restrictionTone({ code: "something_else" }), "warning");
  // Both places that render provider restrictions use the shared rule.
  assert.match(read("../components/publishing-integrations.tsx"), /tone=\{restrictionTone\(item\)\}/);
  assert.match(read("../components/social-publish-sheet.tsx"), /tone=\{restrictionTone\(item\)\}/);
});

// ---------------------------------------------------------------------------
// Migration guard
// ---------------------------------------------------------------------------

test("migrated components use the shared system, not raw status colours", () => {
  const raw = /\b(?:bg|text|border)-(?:red|amber|yellow|orange|emerald|green|sky|blue)-(?:50|100|200|300|500|600|700|800|900|950)\b/;
  for (const path of MIGRATED) {
    const source = read(path);
    const offenders = source.split("\n").filter((line) => raw.test(line) && !line.includes("!bg-amber-400 !text-amber-950") && !line.includes("!bg-emerald-600 !text-white") && !line.includes("bg-red-600/10 text-red-700") && !line.includes("bg-red-700 hover:bg-red-800"));
    assert.deepEqual(offenders, [], `${path} still uses raw status colours`);
  }
});

test("no red or orange text sits on a yellow/amber surface anywhere in the app", () => {
  const files = [...MIGRATED, "../app/page.tsx", "../components/voice-controls.tsx"];
  for (const path of files) {
    for (const line of read(path).split("\n")) {
      if (/bg-(?:amber|yellow)-\d/.test(line)) assert.doesNotMatch(line, /text-(?:red|orange)-\d|text-\[#d94c20\]|--destructive/, `${path}: ${line.trim().slice(0, 120)}`);
    }
  }
  // The filled "warn" publish button uses dark text on amber (white on amber is unreadable).
  assert.match(read("../components/youtube-publish-sheet.tsx"), /warn: "!bg-amber-400 !text-amber-950/);
});
