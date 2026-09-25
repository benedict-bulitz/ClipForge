"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { ArrowLeft, LoaderCircle } from "lucide-react";
import { ApiError, deleteProject, getProject, getProjectGenerationJob } from "@/lib/api";
import { createGenerationWatcher, jobProgressPercent } from "@/lib/generation-poll";
import type { GenerationJob, Project } from "@/lib/types";
import { Brand } from "./brand";
import { ProjectWorkspace } from "./project-workspace";
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
        if (active) setJob(failed);
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

  async function confirmDelete() {
    setDeleting(true);
    try { await deleteProject(projectId); router.push("/"); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "Projekt konnte nicht gelöscht werden."); setDeleteOpen(false); setDeleting(false); }
  }

  if (error && !project && !job) {
    return (
      <main className="theme-app grid min-h-screen place-items-center px-5">
        <div className="cf-surface max-w-md rounded-[24px] border p-7 text-center shadow-sm">
          <Brand />
          <h1 className="mt-6 text-xl font-semibold">Project unavailable</h1>
          <p className="mt-2 text-sm leading-6 text-[#77776d]">{error}</p>
          <Button asChild className="mt-6"><Link href="/"><ArrowLeft className="size-4" /> Back to ClipForge</Link></Button>
        </div>
      </main>
    );
  }
  if (!project) {
    if (job) return <main className="theme-app grid min-h-screen place-items-center px-5"><div className="cf-surface w-full max-w-xl rounded-[24px] border p-7 shadow-sm"><Brand /><h1 className="mt-6 break-words text-xl font-semibold">{job.prompt}</h1><p className="mt-3 text-sm" aria-live="polite">{job.status === "running" ? `Wird erstellt · ${progress}%` : job.status === "completed" ? "Fertig · 100% · Projekt wird geöffnet …" : job.status === "queued" ? `In Warteschlange${job.queue_position ? ` · #${job.queue_position}` : ""}` : "Erstellung fehlgeschlagen"}</p>{(job.status === "running" || job.status === "completed") && <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-[#ff6838]/15" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={progress}><div className="h-full bg-[#ff6838] transition-[width]" style={{ width: `${progress}%` }} /></div>}{job.failure_message && <p className="mt-2 text-sm text-red-700">{job.failure_message}</p>}{(notice || error) && <p role="status" className="mt-2 text-xs text-amber-800">{notice ?? error}</p>}<div className="mt-6 flex gap-2"><Button asChild variant="outline"><Link href="/"><ArrowLeft className="size-4" /> Zur Übersicht</Link></Button>{job.status === "queued" && <Button variant="outline" className="text-red-700" onClick={() => setDeleteOpen(true)}>Projekt löschen</Button>}</div>{deleteOpen && <div className="mt-5 rounded-xl border border-red-200 p-4"><p className="text-sm">Projekt wirklich löschen? Die Warteschlange wird aktualisiert.</p><div className="mt-3 flex gap-2"><Button variant="ghost" onClick={() => setDeleteOpen(false)}>Abbrechen</Button><Button disabled={deleting} onClick={() => void confirmDelete()}>Projekt löschen</Button></div></div>}</div></main>;
    return <main className="theme-app grid min-h-screen place-items-center"><div className="flex items-center gap-3 text-sm font-semibold text-[#77776d]"><LoaderCircle className="size-4 animate-spin text-[#ff6838]" /> Loading project…{notice && <span className="text-xs font-normal text-amber-800">{notice}</span>}</div></main>;
  }
  return <ProjectWorkspace project={project} onProjectChange={setProject} />;
}
