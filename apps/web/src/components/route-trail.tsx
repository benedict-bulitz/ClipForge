"use client";

import { useEffect } from "react";
import { usePathname, useSearchParams } from "next/navigation";
import { recordLinkClick, recordRoute, sameAppReferrer } from "@/lib/navigation";

/**
 * Records which page the current one was opened from (see lib/navigation).
 * Reads Next.js' route, history.length and plain clicks on ClipForge links
 * only: it never listens to, writes or changes browser history.
 */
export function RouteTrail() {
  const pathname = usePathname();
  const search = useSearchParams()?.toString() ?? "";

  useEffect(() => {
    const length = window.history.length;
    recordRoute(`${pathname}${search ? `?${search}` : ""}`, length, Date.now(), sameAppReferrer(document.referrer, window.location.origin, length));
  }, [pathname, search]);

  useEffect(() => {
    function onClick(event: MouseEvent) {
      if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      const link = event.target instanceof Element ? event.target.closest("a[href]") : null;
      if (!(link instanceof HTMLAnchorElement) || (link.target && link.target !== "_self") || link.hasAttribute("download")) return;
      const url = new URL(link.href, window.location.href);
      if (url.origin === window.location.origin) recordLinkClick(url.pathname + url.search);
    }
    document.addEventListener("click", onClick, { capture: true, passive: true });
    return () => document.removeEventListener("click", onClick, { capture: true });
  }, []);

  return null;
}
