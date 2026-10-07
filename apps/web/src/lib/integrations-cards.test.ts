import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { PLATFORMS, callbackNotice, initialCardState, toggleCard } from "./publishing.ts";

const settings = readFileSync(new URL("../components/publishing-integrations.tsx", import.meta.url), "utf8");
const component = settings.slice(settings.indexOf("export function PublishingIntegrations()"), settings.indexOf("function accountStatusTone"));

test("all three platform cards start collapsed", () => {
  assert.deepEqual(initialCardState(), { youtube: false, instagram: false, tiktok: false });
  assert.deepEqual(Object.keys(initialCardState()).sort(), [...PLATFORMS].sort());
});

test("YouTube is not auto-expanded, by its initial state or after loading", () => {
  assert.equal(initialCardState().youtube, false);
  assert.doesNotMatch(settings, /youtube: true/);
  // the lazy initializer is the only source of the initial state
  assert.match(component, /useState<Record<Platform, boolean>>\(initialCardState\)/);
  // nothing opens a card after the accounts load: not connected accounts, not the default account, not an OAuth result
  const effect = component.slice(component.indexOf("useEffect("), component.indexOf("}, [callback]);"));
  assert.doesNotMatch(effect, /setOpen/);
  assert.doesNotMatch(component, /accounts\.length > 0\) result|result\[callback\.platform\]|is_default[^\n]*setOpen/);
  assert.equal((component.match(/setOpen\(/g) ?? []).length, 1); // only the manual toggle
});

test("manual expand/collapse works and leaves the other cards alone", () => {
  const youtube = toggleCard(initialCardState(), "youtube");
  assert.deepEqual(youtube, { youtube: true, instagram: false, tiktok: false });
  const both = toggleCard(youtube, "tiktok");
  assert.deepEqual(both, { youtube: true, instagram: false, tiktok: true });
  assert.deepEqual(toggleCard(both, "youtube"), { youtube: false, instagram: false, tiktok: true });
  assert.deepEqual(youtube, { youtube: true, instagram: false, tiktok: false }); // never mutated
  assert.match(component, /onToggle=\{\(\) => setOpen\(\(current\) => toggleCard\(current, platform\)\)\}/);
  assert.match(settings, /aria-expanded=\{open\}/);
  assert.match(settings, /\{open && \(/);
});

test("a fresh mount resets every card to collapsed", () => {
  // what the user opened on one visit cannot leak into the next mount
  const visited = initialCardState();
  visited.youtube = true;
  visited.instagram = true;
  assert.deepEqual(initialCardState(), { youtube: false, instagram: false, tiktok: false });
  assert.notEqual(initialCardState(), initialCardState());
  // the state lives in the component (unmounted on navigation), never persisted or kept at module level
  assert.doesNotMatch(settings, /localStorage|sessionStorage/);
  assert.doesNotMatch(settings.slice(0, settings.indexOf("export function PublishingIntegrations()")), /let open|const open\b/);
});

test("OAuth success/error notices are still shown, without expanding a card", () => {
  const success = callbackNotice(new URLSearchParams("platform=tiktok&result=connected"));
  assert.deepEqual(success, { platform: "tiktok", tone: "success", text: "TikTok account connected." });
  assert.equal(callbackNotice(new URLSearchParams("youtube=error&reason=access_denied"))?.tone, "error");
  assert.match(component, /if \(callback\) setNotice\(callback\);/);
  assert.match(component, /\{notice && <NoticeLine notice=\{notice\} \/>\}/);
  // a collapsed card header still summarises its accounts and app state
  assert.match(settings, /`\$\{section\.accounts\.length\} account\$\{section\.accounts\.length === 1 \? "" : "s"\}/);
  assert.match(settings, /App not configured/);
});
