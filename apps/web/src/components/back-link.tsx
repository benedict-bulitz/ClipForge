"use client";

import type { ComponentProps } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { backAction, currentTrail, forgetLinkClick, type BackMatch } from "@/lib/navigation";

/**
 * An in-app back control: a real link to `href` (middle-click, new tab and a
 * deep link opened in a fresh tab keep working), but a plain click goes back
 * in the browser's history when this page was opened from the page it names
 * (or, with match="any", from any ClipForge page) - so it behaves like the
 * browser's Back and adds no detour entry.  It only ever starts a navigation
 * on click; it never touches the browser's own Back/Forward.
 */
export function BackLink({ href, match = "exact", onNavigate, ...props }: Omit<ComponentProps<typeof Link>, "href"> & { href: string; match?: BackMatch }) {
  const router = useRouter();
  return (
    <Link
      {...props}
      href={href}
      onNavigate={(event) => {
        onNavigate?.(event);
        // onNavigate runs only for plain same-tab client navigations (not Cmd/Ctrl-click).
        if (backAction(currentTrail(), window.location.pathname, href, match) === "history") {
          event.preventDefault();
          forgetLinkClick();
          router.back();
        }
      }}
    />
  );
}
