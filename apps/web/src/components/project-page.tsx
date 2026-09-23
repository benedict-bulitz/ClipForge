"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { ArrowLeft, LoaderCircle } from "lucide-react";
import { ApiError, deleteProject, getProject, getProjectGenerationJob } from "@/lib/api";
import type { GenerationJob, Project } from "@/lib/types";
import { Brand } from "./brand";
import { ProjectWorkspace } from "./project-workspace";
import { Button } from "./ui/button";

export function ProjectPage({ projectId }: { projectId: string }) {
  const [project, setProject] = useState<Project | null>(null);
  const [job, setJob] = useState<GenerationJob | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);

  useEffect(() => {
    let active = true;
    async function refresh() {
      try {
        const pending = await getProjectGenerationJob(projectId).catch((reason: unknown) => {
          if (reason instanceof ApiError && reason.statusCode === 404) return null;
          throw reason;
        });
        if (pending && (pending.status === "queued" || pending.status === "running")) {
          if (active) { setJob(pending); setProject(null); setError(null); }
          return;
        }
        const value = await getProject(projectId).catch((reason: unknown) => {
          if (pending && reason instanceof ApiError && reason.statusCode === 404) return null;
          throw reason;
        });
        if (active) { setProject(value); setJob(value ? null : pending); setError(null); }
      } catch (reason) {
        if (active) setError(reason instanceof Error ? reason.message : "Project could not be loaded.");
      }
    }
    void refresh();
    const timer = window.setInterval(() => void refresh(), 2500);
    return () => { active = false; window.clearInterval(timer); };
  }, [projectId]);

  async function confirmDelete() {
    setDeleting(true);
    try { await deleteProject(projectId); window.location.assign("/"); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "Projekt konnte nicht gelöscht werden."); setDeleteOpen(false); setDeleting(false); }
  }

  if (error) {
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
    if (job) return <main className="theme-app grid min-h-screen place-items-center px-5"><div className="cf-surface w-full max-w-xl rounded-[24px] border p-7 shadow-sm"><Brand /><h1 className="mt-6 break-words text-xl font-semibold">{job.prompt}</h1><p className="mt-3 text-sm">{job.status === "running" ? `Wird erstellt · ${Math.round(job.progress * 100)}%` : job.status === "queued" ? `In Warteschlange${job.queue_position ? ` · #${job.queue_position}` : ""}` : "Erstellung fehlgeschlagen"}</p>{job.failure_message && <p className="mt-2 text-sm text-red-700">{job.failure_message}</p>}<div className="mt-6 flex gap-2"><Button asChild variant="outline"><Link href="/"><ArrowLeft className="size-4" /> Zur Übersicht</Link></Button>{job.status === "queued" && <Button variant="outline" className="text-red-700" onClick={() => setDeleteOpen(true)}>Projekt löschen</Button>}</div>{deleteOpen && <div className="mt-5 rounded-xl border border-red-200 p-4"><p className="text-sm">Projekt wirklich löschen? Die Warteschlange wird aktualisiert.</p><div className="mt-3 flex gap-2"><Button variant="ghost" onClick={() => setDeleteOpen(false)}>Abbrechen</Button><Button disabled={deleting} onClick={() => void confirmDelete()}>Projekt löschen</Button></div></div>}</div></main>;
    return <main className="theme-app grid min-h-screen place-items-center"><div className="flex items-center gap-3 text-sm font-semibold text-[#77776d]"><LoaderCircle className="size-4 animate-spin text-[#ff6838]" /> Loading project…</div></main>;
  }
  return <ProjectWorkspace project={project} onProjectChange={setProject} />;
}
