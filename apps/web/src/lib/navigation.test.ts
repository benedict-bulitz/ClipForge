import assert from "node:assert/strict";
import test from "node:test";
import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { EMPTY_TRAIL, backAction, currentTrail, forgetLinkClick, nextTrail, recordLinkClick, recordRoute, sameAppReferrer, type RouteTrail } from "./navigation.ts";
import { DEFAULT_FILTERS, filtersUrlUpdate } from "./videos.ts";
import { callbackNotice } from "./publishing.ts";

const read = (path: string) => readFileSync(new URL(path, import.meta.url), "utf8");

/** Replays what the browser does: each step is [url Next.js shows, history.length, link clicked for it]. */
function replay(steps: Array<[string, number, string?]>): RouteTrail {
  return steps.reduce((trail, [url, length, clicked]) => nextTrail(trail, url, length, clicked ?? null), EMPTY_TRAIL);
}

// ---------------------------------------------------------------------------
// The route record: known only after a certain push, never guessed
// ---------------------------------------------------------------------------

test("A → B by a push records A as the page B was opened from", () => {
  assert.deepEqual(replay([["/", 2], ["/videos", 3]]), { current: "/videos", previous: "/", length: 3 });
  assert.deepEqual(replay([["/videos?platform=tiktok", 2], ["/videos/u1", 3]]), { current: "/videos/u1", previous: "/videos", length: 3 });
});

test("Back/Forward never invent a previous page (history.length does not change)", () => {
  // Home → Videos → Settings → Back → Back → Forward → Forward
  let trail = replay([["/", 2], ["/videos", 3], ["/settings/integrations", 4]]);
  assert.equal(trail.previous, "/videos");
  for (const url of ["/videos", "/", "/videos", "/settings/integrations"]) {
    trail = nextTrail(trail, url, 4);
    assert.equal(trail.previous, null, url);
    assert.equal(trail.current, url);
  }
});

test("a document opened from a ClipForge page knows that page (sections are document navigations)", () => {
  // Home → (plain link) Videos: a new document whose referrer is Home
  assert.deepEqual(nextTrail(EMPTY_TRAIL, "/videos", 3, null, sameAppReferrer("http://localhost:3000/", "http://localhost:3000", 3)), { current: "/videos", previous: "/", length: 3 });
  assert.equal(sameAppReferrer("http://localhost:3000/videos?platform=youtube", "http://localhost:3000", 4), "/videos?platform=youtube");
  // no referrer, another site, the OAuth provider, or a new tab without history: unknown
  assert.equal(sameAppReferrer("", "http://localhost:3000", 4), null);
  assert.equal(sameAppReferrer("https://accounts.google.com/", "http://localhost:3000", 4), null);
  assert.equal(sameAppReferrer("http://localhost:8000/api/youtube/oauth/callback", "http://localhost:3000", 4), null);
  assert.equal(sameAppReferrer("http://localhost:3000/videos", "http://localhost:3000", 1), null);
  assert.equal(sameAppReferrer("not a url", "http://localhost:3000", 4), null);
});

test("uncertain cases leave the previous page unknown", () => {
  // first page of a document: fresh tab, deep link, reload, return from OAuth
  assert.equal(replay([["/settings/integrations", 7]]).previous, null);
  // push right after Back: a forward entry was dropped, the length did not grow
  assert.equal(replay([["/", 2], ["/videos", 3], ["/videos/u1", 4], ["/videos", 4], ["/settings/integrations", 4]]).previous, null);
  // two pushes before one was recorded
  assert.equal(replay([["/", 2], ["/settings/integrations", 4]]).previous, null);
  // a replace to another page (e.g. Home after deleting a project)
  assert.equal(replay([["/", 2], ["/projects/p1", 3], ["/", 3]]).previous, null);
});

test("a push right after Back is recognised by the link that was clicked for it", () => {
  // Videos → video → Back → (click) project: the forward entry is dropped, the length stays 4
  const trail = replay([["/", 2], ["/videos", 3], ["/videos/u1", 4], ["/videos", 4], ["/projects/p1", 4, "/projects/p1"]]);
  assert.deepEqual(trail, { current: "/projects/p1", previous: "/videos", length: 4 });
  assert.equal(backAction(trail, "/projects/p1", "/", "any"), "history");
  // a click for another page, or a traversal that happens to follow a click, is not a push
  assert.equal(replay([["/", 2], ["/videos", 3], ["/videos/u1", 4], ["/videos", 4], ["/", 4, "/projects/p1"]]).previous, null);
  // more than one extra entry is never certain
  assert.equal(replay([["/", 2], ["/videos", 4, "/videos"]]).previous, null);
});

test("click evidence is consumed once, expires, and a back control's own click never counts", () => {
  recordRoute("/", 2, 0);
  recordRoute("/videos", 3, 1);
  recordRoute("/videos/u1", 4, 2);
  recordRoute("/videos", 4, 3); // Back
  recordLinkClick("/settings/integrations", 4);
  recordRoute("/settings/integrations", 4, 5);
  assert.deepEqual(currentTrail(), { current: "/settings/integrations", previous: "/videos", length: 4 });
  // a BackLink going back clears its click: the traversal it starts is not a push
  recordLinkClick("/videos", 6);
  forgetLinkClick();
  recordRoute("/videos", 4, 7);
  assert.equal(currentTrail().previous, null);
  // stale evidence (older than 10 s) is ignored
  recordLinkClick("/settings/integrations", 8);
  recordRoute("/settings/integrations", 4, 20_000);
  assert.equal(currentTrail().previous, null);
});

test("query-only replaces (Videos filters, OAuth cleanup) keep what is known", () => {
  assert.deepEqual(replay([["/", 2], ["/videos", 3], ["/videos?platform=youtube", 3], ["/videos?platform=youtube&status=published", 3]]), { current: "/videos", previous: "/", length: 3 });
  assert.deepEqual(replay([["/videos", 2], ["/settings/integrations?youtube=connected", 2]]).previous, null);
  // pushing the same page again: it is its own previous page
  assert.deepEqual(replay([["/videos?platform=youtube", 2], ["/videos", 3]]), { current: "/videos", previous: "/videos", length: 3 });
});

// ---------------------------------------------------------------------------
// In-app back controls
// ---------------------------------------------------------------------------

test("Videos → video → '← Videos' goes back (filters restored by the browser, no new entry)", () => {
  const trail = replay([["/", 2], ["/videos?platform=tiktok", 3], ["/videos/u1", 4]]);
  assert.equal(backAction(trail, "/videos/u1", "/videos"), "history");
});

test("Queue → project and Videos → Settings: the generic ← returns to where the user came from", () => {
  assert.equal(backAction(replay([["/queue", 2], ["/projects/p1", 3]]), "/projects/p1", "/", "any"), "history");
  assert.equal(backAction(replay([["/videos", 2], ["/settings/integrations", 3]]), "/settings/integrations", "/", "any"), "history");
});

test("a control whose target is not the page before follows its link", () => {
  // Settings → Videos → "← Studio": Home was not the previous page
  assert.equal(backAction(replay([["/settings/integrations", 2], ["/videos", 3]]), "/videos", "/"), "link");
  // Home → Videos → "← Studio" is a real Back
  assert.equal(backAction(replay([["/", 2], ["/videos", 3]]), "/videos", "/"), "history");
});

test("fresh tab / deep link: every control has a safe fallback inside ClipForge", () => {
  for (const [page, href, match] of [["/projects/p1", "/", "any"], ["/videos/u1", "/videos", "exact"], ["/settings/integrations", "/", "any"]] as const) {
    assert.equal(backAction(replay([[page, 1]]), page, href, match), "link");
    // even with an outside page before it (another site, the OAuth provider)
    assert.equal(backAction(replay([[page, 5]]), page, href, match), "link");
  }
});

test("a stale record is never used", () => {
  const trail = replay([["/", 2], ["/videos", 3]]);
  assert.equal(backAction(trail, "/settings/integrations", "/", "any"), "link");
  assert.equal(backAction(EMPTY_TRAIL, "/videos", "/"), "link");
});

// ---------------------------------------------------------------------------
// A Videos page restored by Back is never navigated again
// ---------------------------------------------------------------------------

test("the Videos filter sync does nothing when the URL already shows the filters", () => {
  assert.equal(filtersUrlUpdate("/videos", DEFAULT_FILTERS), null);
  assert.equal(filtersUrlUpdate("/videos?status=published&platform=youtube", { ...DEFAULT_FILTERS, status: "published", platform: "youtube" }), null);
  assert.equal(filtersUrlUpdate("/videos", { ...DEFAULT_FILTERS, platform: "youtube" }), "/videos?platform=youtube");
  const library = read("../components/video-library.tsx");
  // never on mount: only after the user changed the filters (a new filters object)
  assert.match(library, /const mountedFilters = useRef\(filters\);/);
  assert.match(library, /if \(filters !== mountedFilters\.current\) \{\s+const target = filtersUrlUpdate\(window\.location\.pathname \+ window\.location\.search, filters\);\s+if \(target\) router\.replace\(target, \{ scroll: false \}\);\s+\}/);
  assert.equal((library.match(/router\.(replace|push)\(/g) ?? []).length, 1);
});

// ---------------------------------------------------------------------------
// Native history is never blocked, rewritten or tied to the Navigation API
// ---------------------------------------------------------------------------

function appSources(): Array<[string, string]> {
  const root = fileURLToPath(new URL("..", import.meta.url));
  const files: Array<[string, string]> = [];
  const walk = (dir: string) => {
    for (const name of readdirSync(dir)) {
      const path = join(dir, name);
      if (statSync(path).isDirectory()) walk(path);
      else if (/\.tsx?$/.test(name) && !name.endsWith(".test.ts")) files.push([path.slice(root.length), readFileSync(path, "utf8")]);
    }
  };
  walk(root);
  return files;
}

const code = (source: string) => source.replace(/\/\*[\s\S]*?\*\/|\/\/[^\n]*/g, "");

test("no code depends on the Navigation API (Safari/WebKit's does not follow this app's history)", () => {
  for (const [path, source] of appSources()) {
    assert.doesNotMatch(code(source), /window\.navigation|\bnavigation\.(entries|currentEntry|canGoBack|addEventListener|navigate|traverseTo|back|forward)\b|currentEntry/, path);
  }
});

test("native Back/Forward/swipe are never intercepted, blocked or rewritten", () => {
  const all = appSources().map(([, source]) => code(source)).join("\n");
  assert.doesNotMatch(all, /addEventListener\(\s*["'](popstate|pageshow|pagehide|beforeunload|unload|hashchange)["']/);
  assert.doesNotMatch(all, /onpopstate|onpageshow|onpagehide|history\.pushState|history\.go\(|history\.forward\(|scrollRestoration|router\.back\(/);
  // the only programmatic traversal: an explicit in-app back button
  assert.equal((all.match(/history\.back\(\)/g) ?? []).length, 1);
  // the only direct history write: the OAuth callback cleanup, inside its guard
  assert.equal((all.match(/history\.replaceState\(/g) ?? []).length, 1);
  assert.match(read("../components/publishing-integrations.tsx"), /if \(callback\) window\.history\.replaceState\(null, "", window\.location\.pathname \+ window\.location\.hash\);/);
  // no custom scroll restoration that could fight a traversal
  assert.equal(existsSync(new URL("../components/use-history-scroll.ts", import.meta.url)), false);
  assert.doesNotMatch(all, /useHistoryScroll|rememberPosition|restoreWhenTallEnough/);
});

test("the route record only reads Next.js' route and history.length; back controls act on click only", () => {
  const trail = read("../components/route-trail.tsx");
  assert.match(trail, /recordRoute\(`\$\{pathname\}\$\{search \? `\?\$\{search\}` : ""\}`, length, Date\.now\(\), sameAppReferrer\(document\.referrer, window\.location\.origin, length\)\);/);
  // only a passive click observer (no history listeners, no history writes, no navigation)
  assert.deepEqual(code(trail).match(/addEventListener\("[a-z]+"/g), ['addEventListener("click"']);
  assert.match(trail, /document\.addEventListener\("click", onClick, \{ capture: true, passive: true \}\);/);
  assert.doesNotMatch(code(trail), /replaceState|pushState|router\.|preventDefault|stopPropagation/);
  assert.match(read("../app/layout.tsx"), /<Suspense fallback=\{null\}><RouteTrail \/><\/Suspense>/);
  const backLink = read("../components/back-link.tsx");
  // a plain anchor: the browser follows it natively unless the previous entry is known
  assert.match(backLink, /return <a \{\.\.\.props\} href=\{href\} onClick=\{handleClick\} \/>;/);
  assert.match(backLink, /if \(event\.defaultPrevented \|\| event\.button !== 0 \|\| event\.metaKey \|\| event\.ctrlKey \|\| event\.shiftKey \|\| event\.altKey\) return;/);
  assert.match(backLink, /if \(backAction\(currentTrail\(\), window\.location\.pathname, href, match\) !== "history"\) return;\s+event\.preventDefault\(\);\s+forgetLinkClick\(\);\s+window\.history\.back\(\);/);
  assert.doesNotMatch(code(backLink), /next\/link|useRouter|router\.|useEffect/);
});

test("every in-app back control uses BackLink with a ClipForge fallback", () => {
  const library = read("../components/video-library.tsx");
  const queue = read("../components/queue-overview.tsx");
  const settingsShell = read("../components/settings-shell.tsx");
  const detail = read("../components/video-detail.tsx");
  const workspace = read("../components/project-workspace.tsx");
  const projectPage = read("../components/project-page.tsx");
  assert.match(library, /<BackLink href="\/"><ArrowLeft className="size-3\.5" \/> <span className="hidden sm:inline">Studio<\/span><\/BackLink>/);
  assert.match(queue, /<BackLink href="\/"><ArrowLeft className="size-3\.5" \/> <span className="hidden sm:inline">Studio<\/span><\/BackLink>/);
  assert.match(settingsShell, /<BackLink href="\/" match="any"><ArrowLeft className="size-3\.5" \/> Back<\/BackLink>/);
  assert.equal((detail.match(/<BackLink href="\/videos">/g) ?? []).length, 3);
  assert.match(workspace, /<BackLink href="\/" match="any" aria-label="Back"/);
  assert.match(projectPage, /<BackLink href="\/" match="any"><ArrowLeft className="size-4" \/> Back to ClipForge<\/BackLink>/);
  assert.match(projectPage, /<BackLink href="\/" match="any"><ArrowLeft className="size-4" \/> Zur Übersicht<\/BackLink>/);
  for (const source of [library, queue, settingsShell, detail, workspace, projectPage]) {
    assert.doesNotMatch(source, /<Link[^>]*>\s*<ArrowLeft/);
  }
});

test("no page redirects Home on its own", () => {
  const workspace = read("../components/project-workspace.tsx");
  const projectPage = read("../components/project-page.tsx");
  // Home only after the project itself was deleted: a native replace, no entry for a dead page
  assert.match(workspace, /else await deleteProject\(project\.id\);\s+setMessages\(\[\]\);[\s\S]{0,120}window\.location\.replace\("\/"\);/);
  assert.match(projectPage, /await deleteProject\(projectId\); window\.location\.replace\("\/"\);/);
  assert.match(read("../app/settings/page.tsx"), /redirect\("\/settings\/integrations"\)/);
  assert.match(read("../app/learning/page.tsx"), /redirect\("\/videos\?project=archived"\)/);
  for (const [path, source] of appSources()) {
    if (path.endsWith("page.tsx") && path.includes("app")) assert.doesNotMatch(code(source), /redirect\("\/"\)/, path);
  }
});

// ---------------------------------------------------------------------------
// Primary sections are ordinary document navigations
// ---------------------------------------------------------------------------

const SECTIONS = ["/", "/videos", "/queue", "/settings/integrations"];

test("every link to Home, Videos, Queue or Settings is a plain anchor (no client-side push)", () => {
  const sectionLink = read("../components/section-link.tsx");
  assert.match(sectionLink, /return <a href=\{href\} \{\.\.\.props\} \/>;/);
  assert.doesNotMatch(code(sectionLink), /onClick=|preventDefault\(|useRouter|next\/link/);
  let sectionLinks = 0;
  for (const [path, source] of appSources()) {
    const body = code(source);
    for (const match of body.matchAll(/<Link\s[^>]*?href=\{?["'`]([^"'`]*)["'`]/g)) {
      const section = match[1].split(/[?#]/, 1)[0] || "/";
      assert.ok(!SECTIONS.includes(section), `${path}: <Link href="${match[1]}"> must be <SectionLink>`);
    }
    assert.doesNotMatch(body, /router\.(push|replace)\(\s*["'`](\/|\/videos|\/queue|\/settings\/integrations)["'`]/, path);
    sectionLinks += (body.match(/<SectionLink[\s>]/g) ?? []).length;
  }
  assert.ok(sectionLinks >= 20, `found ${sectionLinks} section links`);
});

test("the top navigation of every section uses plain anchors", () => {
  const library = read("../components/video-library.tsx");
  const queue = read("../components/queue-overview.tsx");
  const settingsShell = read("../components/settings-shell.tsx");
  const home = read("../app/page.tsx");
  const workspace = read("../components/project-workspace.tsx");
  assert.match(home, /<SectionLink href="\/videos"/);
  assert.match(home, /<SectionLink href="\/settings\/integrations"/);
  assert.match(library, /<SectionLink href="\/settings\/integrations"/);
  assert.match(library, /<SectionLink href="\/" aria-label="ClipForge home">/);
  assert.match(queue, /<SectionLink href="\/videos"/);
  assert.match(queue, /<SectionLink href="\/settings\/integrations"/);
  assert.match(settingsShell, /<SectionLink href="\/videos"/);
  assert.match(settingsShell, /<SectionLink href="\/" aria-label="ClipForge home"/);
  assert.match(workspace, /<SectionLink href="\/videos" aria-label="Videos"/);
});

test("a normal Settings visit never replaces its entry; only real OAuth callback params do", () => {
  const integrations = read("../components/publishing-integrations.tsx");
  assert.equal((code(integrations).match(/replaceState\(/g) ?? []).length, 1);
  assert.match(integrations, /if \(callback\) window\.history\.replaceState\(/);
  assert.equal(callbackNotice(new URLSearchParams("")), null);
  assert.equal(callbackNotice(new URLSearchParams("tab=integrations")), null);
  assert.equal(callbackNotice(new URLSearchParams("platform=tiktok")), null);
  assert.equal(callbackNotice(new URLSearchParams("platform=myspace&result=connected")), null);
  assert.ok(callbackNotice(new URLSearchParams("youtube=connected")));
  assert.ok(callbackNotice(new URLSearchParams("platform=tiktok&result=error&reason=access_denied")));
});
