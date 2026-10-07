import assert from "node:assert/strict";
import test from "node:test";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import {
  backAction,
  historyEntryKey,
  previousAppUrl,
  recallPosition,
  rememberPosition,
  type NavigationLike,
} from "./navigation.ts";

const ORIGIN = "http://localhost:3000";
const read = (path: string) => readFileSync(new URL(path, import.meta.url), "utf8");

/** A tab's history as the browser's Navigation API reports it (same-origin entries only). */
function tab(urls: string[], index = urls.length - 1, key = `k${index}`): NavigationLike {
  return { currentEntry: { index, key }, entries: () => urls.map((url) => ({ url: url.startsWith("http") ? url : `${ORIGIN}${url}` })) };
}

/** What an in-app back control does in that tab. */
function backControl(history: NavigationLike | null, href: string, match: "exact" | "any" = "exact") {
  return backAction(previousAppUrl(history, ORIGIN), href, match);
}

// ---------------------------------------------------------------------------
// The previous history entry comes from the browser, never from a custom stack
// ---------------------------------------------------------------------------

test("the previous ClipForge entry is read from the browser's own history", () => {
  assert.equal(previousAppUrl(tab(["/videos", "/videos/u1"]), ORIGIN)?.pathname, "/videos");
  assert.equal(previousAppUrl(tab(["/", "/videos?platform=tiktok", "/videos/u1"]), ORIGIN)?.search, "?platform=tiktok");
  // fresh tab / direct deep link: there is no previous entry
  assert.equal(previousAppUrl(tab(["/videos/u1"]), ORIGIN), null);
  // a browser without the Navigation API: unknown, so nothing is assumed
  assert.equal(previousAppUrl(null, ORIGIN), null);
  // another site before ClipForge (e.g. the OAuth provider) never counts
  assert.equal(previousAppUrl(tab(["https://accounts.google.com/o/oauth2", "/settings/integrations"]), ORIGIN), null);
  assert.equal(previousAppUrl({ currentEntry: { index: 1 }, entries: () => [{ url: null }, { url: `${ORIGIN}/` }] }, ORIGIN), null);
  assert.equal(previousAppUrl({ currentEntry: { index: 1 }, entries: () => [{ url: "not a url" }, { url: `${ORIGIN}/` }] }, ORIGIN), null);
});

// ---------------------------------------------------------------------------
// In-app back controls behave like Back when the previous page is their target
// ---------------------------------------------------------------------------

test("Videos → video → '← Videos' goes back (filters kept, no extra entry)", () => {
  assert.equal(backControl(tab(["/", "/videos?platform=tiktok&status=published", "/videos/u1"]), "/videos"), "history");
  assert.equal(backControl(tab(["/videos/", "/videos/u1"]), "/videos"), "history");
});

test("a back control whose target is not the previous page follows its link instead", () => {
  // Settings → Videos → "← Studio": Home was not the previous page, so it opens Home
  assert.equal(backControl(tab(["/settings/integrations", "/videos"]), "/"), "link");
  // project → video detail → "← Videos": the list was not the previous page
  assert.equal(backControl(tab(["/projects/p1", "/videos/u1"]), "/videos"), "link");
  // Home → Videos → "← Studio" is a real Back
  assert.equal(backControl(tab(["/", "/videos"]), "/"), "history");
});

test("the project's generic ← returns to wherever the user came from", () => {
  assert.equal(backControl(tab(["/queue", "/projects/p1"]), "/", "any"), "history");
  assert.equal(backControl(tab(["/videos?platform=youtube", "/projects/p1"]), "/", "any"), "history");
  assert.equal(backControl(tab(["/settings/integrations", "/projects/p1"]), "/", "any"), "history");
});

test("a fresh/direct deep link has a safe fallback; a real previous entry is never overridden", () => {
  for (const [href, match] of [["/", "any"], ["/videos", "exact"], ["/", "exact"]] as const) {
    assert.equal(backControl(tab(["/projects/p1"]), href, match), "link");
    assert.equal(backControl(null, href, match), "link");
  }
  // A real previous ClipForge entry wins over the fallback
  assert.equal(backControl(tab(["/queue", "/projects/p1"]), "/", "any"), "history");
});

// ---------------------------------------------------------------------------
// Scroll position: only for the very same history entry
// ---------------------------------------------------------------------------

test("a saved scroll position belongs to one history entry only", () => {
  rememberPosition("entry-videos", { y: 1200, count: 48 });
  assert.deepEqual(recallPosition("entry-videos"), { y: 1200, count: 48 });
  // a new visit to the same URL is a new entry: nothing is restored
  assert.equal(recallPosition("entry-videos-new-visit"), null);
  // without the Navigation API nothing is saved or restored
  rememberPosition(null, { y: 5, count: 1 });
  assert.equal(recallPosition(null), null);
  assert.equal(historyEntryKey(null), null);
  assert.equal(historyEntryKey(tab(["/", "/videos"], 1, "abc")), "abc");
  // bounded: old entries are forgotten
  for (let index = 0; index < 60; index += 1) rememberPosition(`bulk-${index}`, { y: index, count: 0 });
  assert.equal(recallPosition("bulk-0"), null);
  assert.deepEqual(recallPosition("bulk-59"), { y: 59, count: 0 });
});

// ---------------------------------------------------------------------------
// Source contract
// ---------------------------------------------------------------------------

const backLink = read("../components/back-link.tsx");
const hook = read("../components/use-history-scroll.ts");
const library = read("../components/video-library.tsx");
const detail = read("../components/video-detail.tsx");
const workspace = read("../components/project-workspace.tsx");
const projectPage = read("../components/project-page.tsx");
const queue = read("../components/queue-overview.tsx");
const settingsShell = read("../components/settings-shell.tsx");
const integrations = read("../components/publishing-integrations.tsx");

test("browser Back/Forward are never intercepted; in-app back uses router.back()", () => {
  const source = fileURLToPath(new URL("..", import.meta.url));
  const files: string[] = [];
  const walk = (dir: string) => {
    for (const name of readdirSync(dir)) {
      const path = join(dir, name);
      if (statSync(path).isDirectory()) walk(path);
      else if (/\.tsx?$/.test(name) && !name.endsWith(".test.ts")) files.push(path);
    }
  };
  walk(source);
  const all = files.map((path) => readFileSync(path, "utf8")).join("\n");
  assert.doesNotMatch(all, /addEventListener\(["']popstate|onpopstate|history\.pushState|history\.go\(|beforeunload/);
  // the only history write is the intentional OAuth callback cleanup
  assert.equal((all.match(/history\.replaceState\(/g) ?? []).length, 1);
  // BackLink: a real link, router.back() only for plain same-tab clicks with a previous ClipForge page
  assert.match(backLink, /<Link\s+\{\.\.\.props\}\s+href=\{href\}\s+onNavigate=/);
  assert.match(backLink, /=== "history"\) \{\s+event\.preventDefault\(\);\s+router\.back\(\);/);
  assert.doesNotMatch(backLink, /onClick|router\.push|router\.replace/);
});

test("every in-app back control uses BackLink with a fallback target", () => {
  assert.match(library, /<BackLink href="\/"><ArrowLeft className="size-3\.5" \/> <span className="hidden sm:inline">Studio<\/span><\/BackLink>/);
  assert.match(queue, /<BackLink href="\/"><ArrowLeft className="size-3\.5" \/> <span className="hidden sm:inline">Studio<\/span><\/BackLink>/);
  assert.match(settingsShell, /<BackLink href="\/"><ArrowLeft className="size-3\.5" \/> Back to studio<\/BackLink>/);
  assert.equal((detail.match(/<BackLink href="\/videos">/g) ?? []).length, 3);
  assert.match(workspace, /<BackLink href="\/" match="any" aria-label="Back"/);
  assert.match(projectPage, /<BackLink href="\/"><ArrowLeft className="size-4" \/> Back to ClipForge<\/BackLink>/);
  assert.match(projectPage, /<BackLink href="\/"><ArrowLeft className="size-4" \/> Zur Übersicht<\/BackLink>/);
  // no back arrow is a plain Link any more
  for (const source of [library, queue, settingsShell, detail, workspace, projectPage]) {
    assert.doesNotMatch(source, /<Link[^>]*>\s*<ArrowLeft/);
  }
});

test("no page redirects Home on its own; the remaining navigations are deliberate", () => {
  // Home only after the project itself was deleted (replace: the dead page must not stay in history)
  assert.match(workspace, /else await deleteProject\(project\.id\);\s+setMessages\(\[\]\);\s+router\.replace\("\/"\);/);
  assert.match(projectPage, /await deleteProject\(projectId\); router\.replace\("\/"\);/);
  assert.doesNotMatch(projectPage, /router\.push\("\/"\)/);
  // the Videos URL follows its filters without adding entries, and does nothing when it already matches
  assert.match(library, /if \(window\.location\.pathname \+ window\.location\.search !== target\) router\.replace\(target, \{ scroll: false \}\);/);
  // server-side canonical redirects only
  assert.match(read("../app/settings/page.tsx"), /redirect\("\/settings\/integrations"\)/);
  assert.match(read("../app/learning/page.tsx"), /redirect\("\/videos\?project=archived"\)/);
});

test("the OAuth callback cleanup stays an intentional replace and its notice survives React's dev double-run", () => {
  assert.match(integrations, /useState\(\(\) => \(typeof window === "undefined" \? null : callbackNotice\(new URLSearchParams\(window\.location\.search\)\)\)\)/);
  assert.match(integrations, /if \(callback\) window\.history\.replaceState\(null, "", window\.location\.pathname \+ window\.location\.hash\);/);
  assert.match(integrations, /if \(callback\) setNotice\(callback\);/);
  // the callback is no longer read inside the effect (the first dev run would clean the URL and lose it)
  const effect = integrations.slice(integrations.indexOf("useEffect(() => {"), integrations.indexOf("}, [callback]);"));
  assert.doesNotMatch(effect, /new URLSearchParams/);
  // sign-in leaves ClipForge for the provider: a real navigation, kept
  assert.match(integrations, /window\.location\.assign\(authorization_url\)/);
});

test("Videos, Queue and Settings get their scroll position back on Back/Forward", () => {
  assert.match(library, /const restored = useHistoryScroll\(!!page && !loading, items\.length\);/);
  assert.match(library, /restoring \? Math\.min\(MAX_RESTORE, Math\.max\(PAGE_SIZE, restoring\.count\)\) : PAGE_SIZE/);
  assert.match(queue, /useHistoryScroll\(overview !== null \|\| loadError !== null\);/);
  assert.match(read("../components/integrations-settings.tsx"), /useHistoryScroll\(!loading\);/);
  // saved before the next page commits, keyed by the browser's history entry, never fighting the user
  assert.match(hook, /useLayoutEffect\(\(\) => \{\s+\/\/ Read after commit[\s\S]*?const key = historyEntryKey\(\);/);
  assert.match(hook, /if \(!pending\.current\) rememberPosition\(key/);
  assert.match(hook, /USER_SCROLL_EVENTS = \["wheel", "touchstart", "keydown", "pointerdown"\]/);
});
