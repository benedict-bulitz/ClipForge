"use client";

import { useEffect, useState } from "react";
import { ExternalLink, LoaderCircle } from "lucide-react";
import { ApiError, cancelSocialPublication, listSocialPublications, publishMissedNow, retrySocialPublication } from "@/lib/api";
import { PLATFORM_LABELS, activePublications, diagnosticsLine, publicationTone, type SchedulerStatus, type SocialPublication } from "@/lib/publishing";
import { badgeClass, type BadgeTone } from "@/lib/alerts";
import { browserLocale } from "@/lib/youtube";
import { cn } from "@/lib/utils";
import { Alert } from "./ui/alert";
import { Button } from "./ui/button";

/** Publication state -> semantic badge tone (in flight = info, missed = warning). */
const TONE: Record<ReturnType<typeof publicationTone>, BadgeTone> = {
  busy: "info",
  success: "success",
  warn: "warning",
  error: "error",
  muted: "muted",
};

/** This project's Instagram/TikTok posts (each account separately), with their actions. */
export function SocialPublications({ projectId, version, onChanged }: { projectId: string; version: number; onChanged: () => void }) {
  const locale = browserLocale();
  const [items, setItems] = useState<SocialPublication[]>([]);
  const [scheduler, setScheduler] = useState<SchedulerStatus | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    listSocialPublications({ projectId }, controller.signal)
      .then((next) => { setItems(activePublications(next.publications)); setScheduler(next.scheduler); })
      .catch(() => undefined);
    return () => controller.abort();
  }, [projectId, version, tick]);

  // Refresh while something is in flight (bounded by the provider's own answer).
  const inFlight = items.some((item) => ["pending", "uploading", "processing"].includes(item.state));
  useEffect(() => {
    if (!inFlight) return;
    const timer = window.setTimeout(() => setTick((value) => value + 1), 5000);
    return () => window.clearTimeout(timer);
  }, [inFlight, items]);

  if (!items.length) return null;

  async function act(id: string, work: () => Promise<unknown>) {
    setBusy(id);
    setError(null);
    try {
      await work();
      onChanged();
    } catch (reason) {
      setError(reason instanceof ApiError ? reason.message : "The request failed.");
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="mt-4 rounded-[15px] border border-[var(--border)] p-3" aria-label="Instagram and TikTok posts">
      <h3 className="text-[11px] font-bold uppercase tracking-[.12em] text-[var(--muted-foreground)]">Instagram & TikTok</h3>
      <ul className="mt-2 space-y-2">
        {items.map((item) => (
          <li key={item.id} className="flex flex-wrap items-center justify-between gap-2 text-xs">
            <span className="min-w-0">
              <span className="font-semibold">{PLATFORM_LABELS[item.platform]} · {item.account_label.split(" · ").pop()}</span>{" "}
              <span className={cn("ml-1", badgeClass(TONE[publicationTone(item.state)]))}>{item.state_label}</span>
              {item.scheduled_at && item.state === "scheduled" && <span className="ml-1 text-[var(--muted-foreground)]">{new Date(item.scheduled_at).toLocaleString(locale, { timeZone: item.schedule_timezone ?? undefined })}{item.schedule_timezone ? ` (${item.schedule_timezone})` : ""}</span>}
              {item.error?.message && item.state !== "published" && <span className="block text-[11px] text-[var(--muted-foreground)]">{item.error.message}</span>}
              {item.error && item.state !== "published" && diagnosticsLine(item.error.diagnostics) && <span className="mono block select-all text-[10px] text-[var(--muted-foreground)]">{diagnosticsLine(item.error.diagnostics)}</span>}
            </span>
            <span className="flex gap-1.5">
              {item.remote_url && <a href={item.remote_url} target="_blank" rel="noreferrer" className="interactive-text text-xs">Open <ExternalLink className="size-3" /></a>}
              {item.actions.publish_now && <Button size="sm" variant="outline" disabled={!!busy} onClick={() => void act(item.id, () => publishMissedNow(item.id))}>Publish now</Button>}
              {item.actions.retry && <Button size="sm" variant="outline" disabled={!!busy} onClick={() => void act(item.id, () => retrySocialPublication(item.id))}>Retry</Button>}
              {item.actions.cancel && <Button size="sm" variant="ghost" disabled={!!busy} onClick={() => void act(item.id, () => cancelSocialPublication(item.id))}>{busy === item.id ? <LoaderCircle className="size-3.5 animate-spin" /> : null}Cancel</Button>}
            </span>
          </li>
        ))}
      </ul>
      {items.some((item) => item.requires_running_backend) && scheduler && (
        <Alert tone={scheduler.running ? "info" : "warning"} size="sm" role="none" className="mt-2">{scheduler.notice}{!scheduler.running ? " The scheduler is not running right now." : ""}</Alert>
      )}
      {error && <Alert tone="error" size="sm" className="mt-2">{error}</Alert>}
    </div>
  );
}
