/**
 * In-app back controls ("← Videos", "← Studio", the project's ←).
 *
 * The browser's Back/Forward (and the macOS swipe gestures) are authoritative
 * and untouched: ClipForge never listens to or rewrites history traversal and
 * does not use the Navigation API.  Every page-to-page navigation is an
 * ordinary document navigation (see components/page-link.tsx), so the page
 * before the current one is simply the document that linked to it:
 * `document.referrer`, when it is a ClipForge page and the tab has an entry
 * before this one.  A back control goes back in history only then; otherwise
 * (fresh tab, typed URL, another site or the OAuth provider before it) it is
 * an ordinary link to its fallback page.  No history is written, no stack is
 * kept.
 */

/** "exact": only when the previous page is the control's own target page; "any": any ClipForge page. */
export type BackMatch = "exact" | "any";

function pathOf(href: string): string {
  const path = href.split(/[?#]/, 1)[0] || "/";
  return path.length > 1 ? path.replace(/\/+$/, "") : path;
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

/**
 * Whether an in-app back control goes back in history ("history") or follows
 * its link ("link").  Back restores the previous page exactly as it was (e.g.
 * the Videos filters) and adds no entry.
 */
export function backAction(previous: string | null, href: string, match: BackMatch = "exact"): "history" | "link" {
  if (previous === null) return "link";
  if (match === "any") return "history";
  return pathOf(previous) === pathOf(href) ? "history" : "link";
}
