import assert from "node:assert/strict";
import test from "node:test";
import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { backAction, sameAppReferrer } from "./navigation.ts";
import { DEFAULT_FILTERS, filtersUrlUpdate } from "./videos.ts";
import { callbackNotice } from "./publishing.ts";

const ORIGIN = "http://localhost:3000";
const read = (path: string) => readFileSync(new URL(path, import.meta.url), "utf8");

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

/** Source without comments. */
const code = (source: string) => source.replace(/\/\*[\s\S]*?\*\/|\/\/[^\n]*|\{\/\*[\s\S]*?\*\/\}/g, "");
const all = appSources().map(([path, source]) => [path, code(source)] as const);
const occurrences = (pattern: RegExp) => all.flatMap(([path, source]) => [...source.matchAll(pattern)].map((match) => `${path}: ${match[0]}`));

// ---------------------------------------------------------------------------
// In-app back controls: the page that linked here, from the browser itself
// ---------------------------------------------------------------------------

test("the previous page is the ClipForge document that linked here", () => {
  assert.equal(sameAppReferrer(`${ORIGIN}/`, ORIGIN, 3), "/");
  assert.equal(sameAppReferrer(`${ORIGIN}/videos?platform=youtube&status=published`, ORIGIN, 4), "/videos?platform=youtube&status=published");
  // no referrer (typed URL, bookmark), another site, the OAuth provider or the API's callback
  assert.equal(sameAppReferrer("", ORIGIN, 4), null);
  assert.equal(sameAppReferrer("https://accounts.google.com/", ORIGIN, 4), null);
  assert.equal(sameAppReferrer("http://localhost:8000/api/publishing/tiktok/oauth/callback", ORIGIN, 4), null);
  // a new tab opened with Cmd-click has a referrer but no entry to go back to
  assert.equal(sameAppReferrer(`${ORIGIN}/videos`, ORIGIN, 1), null);
  assert.equal(sameAppReferrer("not a url", ORIGIN, 4), null);
});

test("a back control goes back only to the page it names (or any ClipForge page for a generic ←)", () => {
  // Videos (filtered) → video → "← Videos": back restores the exact filtered list
  assert.equal(backAction("/videos?platform=tiktok", "/videos"), "history");
  // Home → Videos → "← Studio"
  assert.equal(backAction("/", "/"), "history");
  // Settings → Videos → "← Studio": Home was not the page before, so it is a link to Home
  assert.equal(backAction("/settings/integrations", "/"), "link");
  // Queue → project ←, Videos → Settings "Back": wherever the user came from inside ClipForge
  assert.equal(backAction("/queue", "/", "any"), "history");
  assert.equal(backAction("/videos", "/", "any"), "history");
  // fresh tab / deep link / outside page before: the fallback link, inside ClipForge
  assert.equal(backAction(null, "/", "any"), "link");
  assert.equal(backAction(null, "/videos"), "link");
});

test("BackLink is a plain anchor: history.back() only when the referrer says so, else the browser follows it", () => {
  const backLink = read("../components/back-link.tsx");
  assert.match(backLink, /return <a \{\.\.\.props\} href=\{href\} onClick=\{handleClick\} \/>;/);
  assert.match(backLink, /if \(event\.defaultPrevented \|\| event\.button !== 0 \|\| event\.metaKey \|\| event\.ctrlKey \|\| event\.shiftKey \|\| event\.altKey\) return;/);
  assert.match(backLink, /const previous = sameAppReferrer\(document\.referrer, window\.location\.origin, window\.history\.length\);\s+if \(backAction\(previous, href, match\) !== "history"\) return;\s+event\.preventDefault\(\);\s+window\.history\.back\(\);/);
  assert.doesNotMatch(code(backLink), /next\/link|useRouter|router\.|useEffect|addEventListener/);
});

// ---------------------------------------------------------------------------
// Every page-to-page navigation is an ordinary document navigation
// ---------------------------------------------------------------------------

test("no next/link and no router.push anywhere: page-to-page navigation creates real browser entries", () => {
  assert.deepEqual(occurrences(/from ["']next\/link["']/g), []);
  assert.deepEqual(occurrences(/<Link[\s>]/g), []);
  assert.deepEqual(occurrences(/router\.(push|back|forward|prefetch)\(/g), []);
  const pageLink = read("../components/page-link.tsx");
  assert.match(pageLink, /return <a href=\{href\} \{\.\.\.props\} \/>;/);
  assert.doesNotMatch(code(pageLink), /onClick=|preventDefault\(|useRouter|next\/link/);
});

test("the remaining Next.js router use is the Videos filter replace (same page, same entry)", () => {
  // Exception, documented: filters are same-page state kept in the URL without a new entry.
  assert.deepEqual(occurrences(/useRouter\(\)/g), ["components/video-library.tsx: useRouter()"]);
  assert.deepEqual(occurrences(/router\.replace\(/g), ["components/video-library.tsx: router.replace("]);
  const library = read("../components/video-library.tsx");
  // never on mount: only after the user changed the filters
  assert.match(library, /const mountedFilters = useRef\(filters\);/);
  assert.match(library, /if \(filters !== mountedFilters\.current\) \{\s+const target = filtersUrlUpdate\(window\.location\.pathname \+ window\.location\.search, filters\);\s+if \(target\) router\.replace\(target, \{ scroll: false \}\);\s+\}/);
  assert.equal(filtersUrlUpdate("/videos?status=published&platform=youtube", { ...DEFAULT_FILTERS, status: "published", platform: "youtube" }), null);
  assert.equal(filtersUrlUpdate("/videos", { ...DEFAULT_FILTERS, platform: "youtube" }), "/videos?platform=youtube");
});

test("programmatic navigation: one real forward page, deliberate replaces, the OAuth provider", () => {
  // generation completed on Home → open the project as a real page (new entry)
  assert.deepEqual(occurrences(/window\.location\.assign\([^)]*\)/g).sort(), [
    "app/page.tsx: window.location.assign(`/projects/${project.id}`)",
    "components/publishing-integrations.tsx: window.location.assign(authorization_url)",
  ]);
  // only after deleting the project itself: Back must not reopen a deleted project
  assert.deepEqual(occurrences(/window\.location\.replace\([^)]*\)/g).sort(), [
    'components/project-page.tsx: window.location.replace("/")',
    'components/project-workspace.tsx: window.location.replace("/")',
  ]);
  assert.match(read("../components/project-page.tsx"), /await deleteProject\(projectId\); window\.location\.replace\("\/"\);/);
  assert.match(read("../components/project-workspace.tsx"), /else await deleteProject\(project\.id\);\s+setMessages\(\[\]\);[\s\S]{0,200}window\.location\.replace\("\/"\);/);
  assert.deepEqual(occurrences(/location\.href\s*=|location\.reload\(/g), []);
});

test("every page link in the UI is a PageLink (projects, videos, sections) or a BackLink", () => {
  const home = read("../app/page.tsx");
  const queue = read("../components/queue-overview.tsx");
  const library = read("../components/video-library.tsx");
  const detail = read("../components/video-detail.tsx");
  // Home: recent projects and its queue items
  assert.match(home, /<PageLink key=\{project\.id\} href=\{`\/projects\/\$\{project\.id\}`\}/);
  assert.equal((home.match(/<PageLink href=\{`\/projects\/\$\{item\.project_id\}`\}/g) ?? []).length, 2);
  // Queue → project
  assert.match(queue, /<PageLink href=\{`\/projects\/\$\{job\.project_id\}`\}>View Details/);
  // Videos → video (thumbnail and title) and → project; video → project
  assert.equal((library.match(/<PageLink href=\{href\}/g) ?? []).length, 2);
  assert.equal((library.match(/\{project && <PageLink href=\{project\}/g) ?? []).length, 2);
  assert.match(detail, /<PageLink href=\{project\}><FolderOpen/);
  // sections
  for (const [path, source] of all) {
    for (const match of source.matchAll(/href=\{?["'`](\/[^"'`]*)["'`]/g)) {
      const tag = source.lastIndexOf("<", match.index);
      assert.match(source.slice(tag, tag + 12), /^<(PageLink|BackLink|a )/, `${path}: ${match[0]} must be a PageLink/BackLink`);
    }
  }
});

// ---------------------------------------------------------------------------
// Native history is never blocked, rewritten or tied to the Navigation API
// ---------------------------------------------------------------------------

test("native Back/Forward/swipe are never intercepted; history is written only by the OAuth cleanup", () => {
  assert.deepEqual(occurrences(/addEventListener\(\s*["'](popstate|pageshow|pagehide|beforeunload|unload|hashchange)["']/g), []);
  assert.deepEqual(occurrences(/onpopstate|onpageshow|onpagehide|history\.pushState|history\.go\(|history\.forward\(|scrollRestoration/g), []);
  assert.deepEqual(occurrences(/history\.replaceState\(/g), ["components/publishing-integrations.tsx: history.replaceState("]);
  // the only programmatic traversal: an explicit in-app back button
  assert.deepEqual(occurrences(/history\.back\(\)/g), ["components/back-link.tsx: history.back()"]);
  assert.deepEqual(occurrences(/window\.navigation|currentEntry|navigation\.entries/g), []);
  for (const removed of ["../components/use-history-scroll.ts", "../components/route-trail.tsx"]) {
    assert.equal(existsSync(new URL(removed, import.meta.url)), false, removed);
  }
  assert.deepEqual(occurrences(/useHistoryScroll|RouteTrail|recordLinkClick/g), []);
});

test("a normal Settings visit never replaces its entry; only real OAuth callback params do", () => {
  const integrations = read("../components/publishing-integrations.tsx");
  assert.match(integrations, /if \(callback\) window\.history\.replaceState\(null, "", window\.location\.pathname \+ window\.location\.hash\);/);
  assert.equal(callbackNotice(new URLSearchParams("")), null);
  assert.equal(callbackNotice(new URLSearchParams("platform=tiktok")), null);
  assert.equal(callbackNotice(new URLSearchParams("platform=myspace&result=connected")), null);
  assert.ok(callbackNotice(new URLSearchParams("youtube=connected")));
  assert.ok(callbackNotice(new URLSearchParams("platform=tiktok&result=error&reason=access_denied")));
});

test("server redirects are canonical only; no page redirects Home", () => {
  assert.match(read("../app/settings/page.tsx"), /redirect\("\/settings\/integrations"\)/);
  assert.match(read("../app/learning/page.tsx"), /redirect\("\/videos\?project=archived"\)/);
  assert.deepEqual(occurrences(/redirect\(["'`]\/["'`]\)/g), []);
});

test("every in-app back control is a BackLink with a ClipForge fallback", () => {
  const detail = read("../components/video-detail.tsx");
  assert.match(read("../components/video-library.tsx"), /<BackLink href="\/"><ArrowLeft className="size-3\.5" \/> <span className="hidden sm:inline">Studio<\/span><\/BackLink>/);
  assert.match(read("../components/queue-overview.tsx"), /<BackLink href="\/"><ArrowLeft className="size-3\.5" \/> <span className="hidden sm:inline">Studio<\/span><\/BackLink>/);
  assert.match(read("../components/settings-shell.tsx"), /<BackLink href="\/" match="any"><ArrowLeft className="size-3\.5" \/> Back<\/BackLink>/);
  assert.equal((detail.match(/<BackLink href="\/videos">/g) ?? []).length, 3);
  assert.match(read("../components/project-workspace.tsx"), /<BackLink href="\/" match="any" aria-label="Back"/);
  assert.match(read("../components/project-page.tsx"), /<BackLink href="\/" match="any"><ArrowLeft className="size-4" \/> Zur Übersicht<\/BackLink>/);
});
