"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { ArrowLeft, LoaderCircle } from "lucide-react";
import { getProject } from "@/lib/api";
import type { Project } from "@/lib/types";
import { Brand } from "./brand";
import { ProjectWorkspace } from "./project-workspace";
import { Button } from "./ui/button";

export function ProjectPage({ projectId }: { projectId: string }) {
  const [project, setProject] = useState<Project | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    getProject(projectId)
      .then((value) => { if (active) setProject(value); })
      .catch((reason) => { if (active) setError(reason instanceof Error ? reason.message : "Project could not be loaded."); });
    return () => { active = false; };
  }, [projectId]);

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
    return <main className="theme-app grid min-h-screen place-items-center"><div className="flex items-center gap-3 text-sm font-semibold text-[#77776d]"><LoaderCircle className="size-4 animate-spin text-[#ff6838]" /> Loading project…</div></main>;
  }
  return <ProjectWorkspace project={project} onProjectChange={setProject} />;
}
