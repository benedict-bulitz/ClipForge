"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { ArrowLeft, ExternalLink, LoaderCircle } from "lucide-react";
import { getLearningArchiveEntry, listLearningArchive } from "@/lib/api";
import { formatDateTime, formatMetric, type ArchiveEntry, type PerformanceReport } from "@/lib/youtube";
import { Brand } from "./brand";
import { Button } from "./ui/button";
import { ThemeToggle } from "./theme-toggle";
import { PerformanceSection } from "./youtube-panel";

type Detail = ArchiveEntry & { performance: PerformanceReport; fingerprint: Record<string, Record<string, unknown> | null> };

/** Read-only: archived videos can be analysed, never edited or re-rendered. */
export function LearningHistory() {
  const [entries, setEntries] = useState<ArchiveEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [detail, setDetail] = useState<Detail | null>(null);

  useEffect(() => {
    let active = true;
    listLearningArchive()
      .then((result) => { if (active) setEntries(result.entries); })
      .catch(() => { if (active) setError("The Learning History could not be loaded."); });
    return () => { active = false; };
  }, []);

  function toggle(uploadId: string) {
    if (open === uploadId) { setOpen(null); return; }
    setOpen(uploadId);
    setDetail(null);
    getLearningArchiveEntry(uploadId).then(setDetail).catch(() => setError("This record could not be loaded."));
  }

  return (
    <main className="theme-app min-h-screen bg-[var(--background)]">
      <header className="sticky top-0 z-40 border-b border-[var(--border)] bg-[var(--surface)] backdrop-blur-xl">
        <div className="mx-auto flex min-h-16 max-w-[1100px] items-center justify-between gap-4 px-4 sm:px-6">
          <Link href="/" aria-label="ClipForge home"><Brand /></Link>
          <div className="flex items-center gap-2">
            <ThemeToggle />
            <Button asChild variant="ghost" size="sm"><Link href="/"><ArrowLeft className="size-3.5" /> Back to studio</Link></Button>
          </div>
        </div>
      </header>
      <div className="mx-auto max-w-[1100px] px-4 pb-20 pt-8 sm:px-6">
        <p className="mono text-[10px] font-medium uppercase tracking-[.14em] text-[#ff6838]">Analytics archive</p>
        <h1 className="mt-2 text-3xl font-semibold tracking-[-.045em]">Learning History</h1>
        <p className="mt-2 max-w-2xl text-sm text-[var(--muted-foreground)]">Uploaded videos whose local project was deleted. Their media is gone; ClipForge keeps only the compact performance record so future videos can learn from it. These videos cannot be edited here.</p>
        {error && <p role="alert" className="mt-5 rounded-xl bg-red-50 px-3 py-2 text-sm text-red-800">{error}</p>}
        {!entries && !error && <p className="mt-6 flex items-center gap-2 text-sm text-[var(--muted-foreground)]"><LoaderCircle className="size-4 animate-spin" /> Loading…</p>}
        {entries && entries.length === 0 && <p className="mt-6 text-sm text-[var(--muted-foreground)]">No archived videos yet.</p>}
        <ul className="mt-6 space-y-3">
          {(entries ?? []).map((entry) => (
            <li key={entry.upload_id} className="workspace-card p-4">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0">
                  <p className="font-semibold">{entry.title}</p>
                  <p className="mt-0.5 text-xs text-[var(--muted-foreground)]">
                    {entry.upload.published_at ? `Published ${formatDateTime(entry.upload.published_at)}` : "Not published"} · Views {formatMetric("views", entry.summary.views === null ? undefined : { value: entry.summary.views, availability: "available", reason: null, source: "youtube_analytics_api" })} · Avg view {entry.summary.averageViewPercentage === null ? "–" : `${entry.summary.averageViewPercentage.toFixed(1)}%`}
                  </p>
                </div>
                <div className="flex items-center gap-1">
                  {entry.upload.shorts_url && <a href={entry.upload.shorts_url} target="_blank" rel="noreferrer" className="interactive-text"><ExternalLink className="size-3.5" /> YouTube</a>}
                  <button type="button" className="interactive-text" aria-expanded={open === entry.upload_id} onClick={() => toggle(entry.upload_id)}>{open === entry.upload_id ? "Hide" : "Performance"}</button>
                </div>
              </div>
              {open === entry.upload_id && (detail?.upload_id === entry.upload_id ? (
                <>
                  <PerformanceSection report={detail.performance} />
                  <dl className="mt-4 grid gap-2 text-[11px] sm:grid-cols-3">
                    <div><dt className="text-[var(--muted-foreground)]">Hook</dt><dd className="font-semibold">{String((detail.fingerprint.hook ?? {}).strategy ?? "–").replaceAll("_", " ")}</dd></div>
                    <div><dt className="text-[var(--muted-foreground)]">Format</dt><dd className="font-semibold">{String((detail.fingerprint.content ?? {}).format ?? "–")}</dd></div>
                    <div><dt className="text-[var(--muted-foreground)]">Scenes</dt><dd className="font-semibold">{String((detail.fingerprint.pacing ?? {}).scene_count ?? "–")}</dd></div>
                  </dl>
                </>
              ) : <p className="mt-3 flex items-center gap-2 text-xs text-[var(--muted-foreground)]"><LoaderCircle className="size-3.5 animate-spin" /> Loading…</p>)}
            </li>
          ))}
        </ul>
      </div>
    </main>
  );
}
