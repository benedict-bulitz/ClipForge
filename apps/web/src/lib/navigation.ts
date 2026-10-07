/**
 * In-app back controls ("← Videos", "← Studio", the project's ←).
 *
 * The browser's Back/Forward (and the macOS swipe gestures) are authoritative
 * and untouched: ClipForge never listens to or rewrites history traversal, and
 * does not use the Navigation API (window.navigation), whose WebKit/Safari
 * implementation does not follow this app's history (entries are not updated
 * by pushState, every entry reports the same key, currentEntry becomes null
 * after Back).
 *
 * An in-app back control only needs to know the page that was open right
 * before the current one *when the current page was opened by a push*.  That
 * is recorded from Next.js' own route changes plus `history.length` (which
 * grows only when an entry is pushed):
 *
 * - the first page of a document: the same-origin page that linked to it
 *   (document.referrer - primary sections are ordinary document
 *   navigations), else unknown (fresh tab, typed URL, return from OAuth);
 * - a different page with exactly one more history entry: a push -
 *   previous = last page;
 * - a different page that an in-app link was just clicked for, with no more
 *   than one extra entry: a push too (right after Back the browser drops the
 *   forward entries, so the length need not grow);
 * - a different page otherwise (Back/Forward, a replace): previous unknown;
 * - the same page with new query params and the same length: a replace (the
 *   Videos filters) - unchanged.
 *
 * When the previous page is unknown, the control simply follows its link -
 * it never guesses, so the worst case is the old behaviour, never a wrong
 * history jump.  No history entry is written and no stack is kept.
 */

export type RouteTrail = {
  /** Pathname of the current page as last seen, or null before the first page. */
  current: string | null;
  /** Pathname of the page this one was pushed from, when known. */
  previous: string | null;
  /** history.length when the current page was recorded. */
  length: number;
};

export const EMPTY_TRAIL: RouteTrail = { current: null, previous: null, length: 0 };

/** "exact": only when the previous page is the control's own target page; "any": any ClipForge page. */
export type BackMatch = "exact" | "any";

function pathOf(href: string): string {
  const path = href.split(/[?#]/, 1)[0] || "/";
  return path.length > 1 ? path.replace(/\/+$/, "") : path;
}

/**
 * The trail after Next.js showed `href` with `historyLength` entries in this
 * tab; `clicked` is the page an in-app link was just clicked for, if any.
 */
export function nextTrail(trail: RouteTrail, href: string, historyLength: number, clicked: string | null = null, referrer: string | null = null): RouteTrail {
  const path = pathOf(href);
  if (trail.current === null) return { current: path, previous: referrer === null ? null : pathOf(referrer), length: historyLength };
  // Exactly one more entry: one push from the recorded page.  A clicked link to exactly this page
  // is a push even when Back dropped forward entries.  Anything else (Back/Forward, a replace,
  // two quick pushes) is not certain.
  const pushed = historyLength === trail.length + 1 || (clicked !== null && pathOf(clicked) === path && historyLength <= trail.length + 1);
  if (path === trail.current) {
    // Same page: a query-only replace (filters) keeps what is known; a push of the same page is its own previous.
    return pushed ? { current: path, previous: path, length: historyLength } : { ...trail, length: historyLength };
  }
  return { current: path, previous: pushed ? trail.current : null, length: historyLength };
}

/**
 * Whether an in-app back control goes back in history ("history") or follows
 * its link ("link").  Back restores the previous page exactly as it was (e.g.
 * the Videos filters) and adds no entry.
 */
export function backAction(trail: RouteTrail, currentPath: string, href: string, match: BackMatch = "exact"): "history" | "link" {
  if (!trail.previous || trail.current !== pathOf(currentPath)) return "link";
  if (match === "any") return "history";
  return trail.previous === pathOf(href) ? "history" : "link";
}

// The tab's trail and the last in-app link click (module state: reset by every full page load).
let trail: RouteTrail = EMPTY_TRAIL;
let click: { href: string; at: number } | null = null;
const CLICK_EVIDENCE_MS = 10_000;

export function recordRoute(href: string, historyLength: number, now = Date.now(), referrer: string | null = null): void {
  const clicked = click && now - click.at <= CLICK_EVIDENCE_MS ? click.href : null;
  click = null;
  trail = nextTrail(trail, href, historyLength, clicked, referrer);
}

/**
 * The ClipForge page that linked to this document, when the tab has an entry
 * before it (a new tab opened with Cmd-click has a referrer but no history).
 */
export function sameAppReferrer(referrer: string, origin: string, historyLength: number): string | null {
  if (!referrer || historyLength < 2) return null;
  try {
    const url = new URL(referrer);
    return url.origin === origin ? url.pathname + url.search : null;
  } catch {
    return null;
  }
}

/** An in-app link was clicked (plain same-tab click on a ClipForge link). */
export function recordLinkClick(href: string, now = Date.now()): void {
  click = { href, at: now };
}

/** A back control is going back in history: its click is not a push. */
export function forgetLinkClick(): void {
  click = null;
}

export function currentTrail(): RouteTrail {
  return trail;
}
