"use client";

import type { ComponentProps } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { backAction, browserNavigation, previousAppUrl, type BackMatch } from "@/lib/navigation";

/**
 * An in-app back control: a real link to `href` (middle-click, new tab and a
 * deep link opened in a fresh tab keep working), but a plain click goes back
 * in the browser's history when the previous entry is the page it names - so
 * it behaves like the browser's Back and never adds a detour entry.
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
        if (backAction(previousAppUrl(browserNavigation(), window.location.origin), href, match) === "history") {
          event.preventDefault();
          router.back();
        }
      }}
    />
  );
}
