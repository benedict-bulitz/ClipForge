import type { AnchorHTMLAttributes } from "react";

/** ClipForge's primary sections: Home, Videos, Queue and Settings. */
export type Section = "/" | "/videos" | "/queue" | "/settings/integrations";

/**
 * Navigation between primary sections is an ordinary document navigation:
 * a plain <a href> with no click handling, so the browser itself creates the
 * history entry (Safari skipped script-created entries between sections on
 * Back).  Use next/link only for navigation inside a section.
 */
export function SectionLink({ href, ...props }: Omit<AnchorHTMLAttributes<HTMLAnchorElement>, "href" | "onClick"> & { href: Section | `/videos?${string}` | `/settings/integrations#${string}` }) {
  return <a href={href} {...props} />;
}
