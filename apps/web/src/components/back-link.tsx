"use client";

import type { AnchorHTMLAttributes, MouseEvent } from "react";
import { backAction, currentTrail, forgetLinkClick, type BackMatch } from "@/lib/navigation";

/**
 * An in-app back control.  A plain click goes back in the browser's history
 * (history.back()) when the previous entry is known to be the page it names
 * - or, with match="any", any ClipForge page; otherwise it is an ordinary
 * link to `href` that the browser follows natively.  It never touches the
 * browser's own Back/Forward.
 */
export function BackLink({ href, match = "exact", onClick, ...props }: Omit<AnchorHTMLAttributes<HTMLAnchorElement>, "href"> & { href: string; match?: BackMatch }) {
  function handleClick(event: MouseEvent<HTMLAnchorElement>) {
    onClick?.(event);
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    if (backAction(currentTrail(), window.location.pathname, href, match) !== "history") return;
    event.preventDefault();
    forgetLinkClick();
    window.history.back();
  }
  return <a {...props} href={href} onClick={handleClick} />;
}
