/**
 * In-app back controls ("← Videos", "← Studio", the project's ← arrow) and
 * the browser's own history.
 *
 * The browser's Back/Forward (and the macOS swipe gestures) are authoritative;
 * ClipForge never intercepts them.  The in-app controls used to be plain links
 * that always *pushed* their target ("/" or "/videos"): they went Home even
 * when the user came from Videos or the Queue, dropped the Videos filters, and
 * filled the history with extra Home entries, so a later swipe-back walked
 * through those detours instead of the path the user actually took.
 *
 * Now an in-app back control goes *back in history* when the previous entry in
 * this tab is the page it names (or, for a generic ←, any ClipForge page), and
 * only falls back to its link when there is no such entry - a deep link opened
 * in a fresh tab, or a browser without the Navigation API.  No custom
 * navigation stack: the previous entry is read from the browser itself.
 */

/** The subset of the browser Navigation API (window.navigation) used here. */
export type NavigationLike = {
  currentEntry: { index: number; key?: string } | null;
  entries(): Array<{ url: string | null }>;
};

/** "exact": only when the previous page is the control's own target page; "any": any ClipForge page. */
export type BackMatch = "exact" | "any";

/** The previous history entry of this tab when it is a ClipForge page; null when unknown or not ClipForge. */
export function previousAppUrl(navigation: NavigationLike | null | undefined, origin: string): URL | null {
  const index = navigation?.currentEntry?.index;
  if (!navigation || index === undefined || index < 1) return null;
  const url = navigation.entries()[index - 1]?.url;
  if (!url) return null;
  try {
    const parsed = new URL(url);
    return parsed.origin === origin ? parsed : null;
  } catch {
    return null;
  }
}

function pathOf(href: string): string {
  const path = href.split(/[?#]/, 1)[0] || "/";
  return path.length > 1 ? path.replace(/\/+$/, "") : path;
}

/**
 * Whether an in-app back control should go back in history ("history") or
 * follow its link ("link").  Going back restores the previous page exactly as
 * it was in the URL (e.g. the Videos filters) instead of a fresh, unfiltered
 * copy, and adds no history entry.
 */
export function backAction(previous: URL | null, href: string, match: BackMatch = "exact"): "history" | "link" {
  if (!previous) return "link";
  if (match === "any") return "history";
  return pathOf(previous.pathname) === pathOf(href) ? "history" : "link";
}

/** The browser's Navigation API, when this browser has it. */
export function browserNavigation(): NavigationLike | null {
  if (typeof window === "undefined") return null;
  const navigation = (window as unknown as { navigation?: NavigationLike }).navigation;
  return navigation && typeof navigation.entries === "function" ? navigation : null;
}

// ---------------------------------------------------------------------------
// Scroll position per history entry (Back/Forward only)
// ---------------------------------------------------------------------------

/** Where a page was, plus how many list items were loaded (to reload enough to reach it). */
export type SavedPosition = { y: number; count: number };

const positions = new Map<string, SavedPosition>();
const MAX_POSITIONS = 50;

/**
 * The browser's id of the current history entry.  It is stable when Back or
 * Forward returns to the entry and new for every new visit, so a saved
 * position is only ever restored for the very same entry - never on a fresh
 * visit.  Null without the Navigation API (then nothing is restored).
 */
export function historyEntryKey(navigation: NavigationLike | null = browserNavigation()): string | null {
  return navigation?.currentEntry?.key ?? null;
}

export function rememberPosition(key: string | null, position: SavedPosition): void {
  if (!key) return;
  positions.delete(key);
  positions.set(key, position);
  while (positions.size > MAX_POSITIONS) positions.delete(positions.keys().next().value as string);
}

export function recallPosition(key: string | null): SavedPosition | null {
  return key ? positions.get(key) ?? null : null;
}
