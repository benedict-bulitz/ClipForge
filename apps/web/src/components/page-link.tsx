import type { AnchorHTMLAttributes } from "react";

/** A ClipForge page: Home, Videos, Queue, Settings, a video or a project. */
export type PageHref = `/${string}`;

/**
 * Every navigation from one ClipForge page to another is an ordinary document
 * navigation: a plain <a href> with no click handling, so the browser itself
 * creates the history entry.  (Safari skipped entries that Next.js created
 * with history.pushState for client-side navigations: Back/Forward jumped
 * over them.)  Never use next/link or router.push for page-to-page navigation.
 */
export function PageLink({ href, ...props }: Omit<AnchorHTMLAttributes<HTMLAnchorElement>, "href" | "onClick"> & { href: PageHref }) {
  return <a href={href} {...props} />;
}
