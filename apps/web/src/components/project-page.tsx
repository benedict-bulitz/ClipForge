"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { ArrowLeft, LoaderCircle } from "lucide-react";
import { ApiError, cancelGenerationJob, deleteProject, getProject, getProjectGenerationJob } from "@/lib/api";
import { applyJob, CANCEL_LABEL, CANCELLED_LABEL, CANCELLING_LABEL, cancelView, createCancelRequester, markCancelling, type CancelRequester } from "@/lib/generation-cancel";
import { createGenerationWatcher, generationTimeLabel, jobProgressPercent } from "@/lib/generation-poll";
import type { GenerationJob, Project } from "@/lib/types";
import { Brand } from "./brand";
import { ProjectWorkspace } from "./project-workspace";
import { BackLink } from "./back-link";
import { Button } from "./ui/button";

function missingAsNull(reason: unknown): null {
  if (reason instanceof ApiError && reason.statusCode === 404) return null;
  throw reason;
}

export function ProjectPage({ projectId }: { projectId: string }) {
  const router = useRouter();
  const [project, setProject] = useState<Project | null>(null);
  const [job, setJob] = useState<GenerationJob | null>(null);
  const [progress, setProgress] = useState(0);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const lastJob = useRef<GenerationJob | null>(null);
  const [cancelPending, setCancelPending] = useState<ReadonlySet<string>>(new Set());
  const cancelRequester = useRef<CancelRequester | null>(null);

  useEffect(() => {
    let active = true;
    lastJob.current = null;
    // One sequential, time-bounded watcher: it stops as soon as the project
    // is shown, so a late response can never put a finished project back
    // into a stale "running" view.
    const watcher = createGenerationWatcher<Project>({
      loadJob: (signal) => getProjectGenerationJob(projectId, signal).catch(missingAsNull),
      loadProject: (signal) =>
        getProject(projectId, signal).catch((reason: unknown) => {
          // A queued job's project row may not exist yet.
          if (lastJob.current && reason instanceof ApiError && reason.statusCode === 404) return null;
          throw reason;
        }),
      onJob: (next) => {
        if (!active) return;
        lastJob.current = next;
        setJob(next);
        setProgress((previous) => jobProgressPercent(next, previous));
      },
      onCompleted: (value) => {
        if (!active) return;
        setProgress(100);
        setProject(value);
        setJob(null);
        setNotice(null);
        setError(null);
      },
      onFailed: (failed) => {
        if (!active) return;
        setJob(failed);
        if (failed.status !== "cancelled") return;
        // A cancelled generation keeps its project when one was already created: open it.
        void getProject(projectId).then((value) => { if (active) { setProject(value); setJob(null); } }).catch(() => undefined);
      },
      onError: (message) => {
        if (!active) return;
        // Before anything was shown, a failure is the page's error; later it
        // is a small notice while polling continues.
        if (message && !lastJob.current) setError(message);
        else setNotice(message);
        if (!message) setError(null);
      },
    });
    watcher.start();
    return () => {
      active = false;
      watcher.stop();
    };
  }, [projectId]);

  function cancelRunning(jobId: string) {
    cancelRequester.current ??= createCancelRequester({
      cancel: cancelGenerationJob,
      onPending: setCancelPending,
      onJob: (next) => setJob((current) => (current ? applyJob([current], next)[0] : current)),
      onError: setError,
    });
    if (cancelRequester.current.request(jobId)) setJob((current) => (current ? markCancelling([current], jobId)[0] : current));
  }

  async function confirmDelete() {
    setDeleting(true);
    try { await deleteProject(projectId); router.replace("/"); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "Projekt konnte nicht gelöscht werden."); setDeleteOpen(false); setDeleting(false); }
  }

  if (error && !project && !job) {
    return (
      <main className="theme-app grid min-h-screen place-items-center px-5">
        <div className="cf-surface max-w-md rounded-[24px] border p-7 text-center shadow-sm">
          <Brand />
          <h1 className="mt-6 text-xl font-semibold">Project unavailable</h1>
          <p className="mt-2 text-sm leading-6 text-[#77776d]">{error}</p>
          <Button asChild className="mt-6"><BackLink href="/" match="any"><ArrowLeft className="size-4" /> Back to ClipForge</BackLink></Button>
        </div>
      </main>
    );
  }
  if (!project) {
    if (job) return <main className="theme-app grid min-h-screen place-items-center px-5"><div className="cf-surface w-full max-w-xl rounded-[24px] border p-7 shadow-sm"><Brand /><h1 className="mt-6 break-words text-xl font-semibold">{job.prompt}</h1><p className="mt-3 text-sm" aria-live="polite">{cancelView(job, cancelPending) === "cancelling" ? `${CANCELLING_LABEL} · ${progress}%` : job.status === "cancelled" ? CANCELLED_LABEL : job.status === "running" ? `Wird erstellt · ${progress}%` : job.status === "completed" ? "Fertig · 100% · Projekt wird geöffnet …" : job.status === "queued" ? `In Warteschlange${job.queue_position ? ` · #${job.queue_position}` : ""}` : "Erstellung fehlgeschlagen"}</p>{(job.status === "running" || job.status === "completed") && <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-[#ff6838]/15" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={progress}><div className="h-full bg-[#ff6838] transition-[width]" style={{ width: `${progress}%` }} /></div>}{generationTimeLabel(job) && <div className="generation-timing mt-3" aria-label="Generation time"><p className="mono">{generationTimeLabel(job)}</p>{job.status === "running" && job.stage_label && <p className="cf-text-meta">{job.stage_label}</p>}</div>}{job.failure_message && job.status !== "cancelled" && <p className="cf-text-error mt-2 text-sm">{job.failure_message}</p>}{(notice || error) && <p role={error && !notice ? "alert" : "status"} className={`mt-2 text-xs ${error && !notice ? "cf-text-error" : "cf-text-warning"}`}>{notice ?? error}</p>}<div className="mt-6 flex gap-2"><Button asChild variant="outline"><BackLink href="/" match="any"><ArrowLeft className="size-4" /> Zur Übersicht</BackLink></Button>{job.status === "queued" && <Button variant="outline" className="cf-text-error" onClick={() => setDeleteOpen(true)}>Projekt löschen</Button>}{cancelView(job, cancelPending) === "cancel" && <Button variant="outline" className="cf-text-error" onClick={() => cancelRunning(job.id)}>{CANCEL_LABEL}</Button>}{cancelView(job, cancelPending) === "cancelling" && <Button variant="outline" disabled><LoaderCircle className="size-4 animate-spin" /> {CANCELLING_LABEL}</Button>}</div>{deleteOpen && <div className="cf-tone-error mt-5 rounded-xl border p-4"><p className="text-sm">Projekt wirklich löschen? Die Warteschlange wird aktualisiert.</p><div className="mt-3 flex gap-2"><Button variant="ghost" onClick={() => setDeleteOpen(false)}>Abbrechen</Button><Button disabled={deleting} onClick={() => void confirmDelete()}>Projekt löschen</Button></div></div>}</div></main>;
    return <main className="theme-app grid min-h-screen place-items-center"><div className="flex items-center gap-3 text-sm font-semibold text-[#77776d]"><LoaderCircle className="size-4 animate-spin text-[#ff6838]" /> Loading project…{notice && <span className="cf-text-warning text-xs font-normal">{notice}</span>}</div></main>;
  }
  return <ProjectWorkspace project={project} onProjectChange={setProject} />;
}
