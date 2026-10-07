"use client";

import type { AnchorHTMLAttributes, MouseEvent } from "react";
import { backAction, sameAppReferrer, type BackMatch } from "@/lib/navigation";
import type { PageHref } from "./page-link";

/**
 * An in-app back control.  A plain click goes back in the browser's history
 * (history.back()) when the page that linked here is the page it names - or,
 * with match="any", any ClipForge page; otherwise it is an ordinary link to
 * `href` that the browser follows natively.  It never touches the browser's
 * own Back/Forward.
 */
export function BackLink({ href, match = "exact", onClick, ...props }: Omit<AnchorHTMLAttributes<HTMLAnchorElement>, "href"> & { href: PageHref; match?: BackMatch }) {
  function handleClick(event: MouseEvent<HTMLAnchorElement>) {
    onClick?.(event);
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const previous = sameAppReferrer(document.referrer, window.location.origin, window.history.length);
    if (backAction(previous, href, match) !== "history") return;
    event.preventDefault();
    window.history.back();
  }
  return <a {...props} href={href} onClick={handleClick} />;
}
