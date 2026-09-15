"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { ArrowRight, Clock3, CornerDownLeft, LoaderCircle, Plus } from "lucide-react";
import { createProject, listProjects, renderProject } from "@/lib/api";
import type { Project } from "@/lib/types";
import { AdvancedOptions } from "@/components/advanced-options";
import { Brand } from "@/components/brand";
import { Button } from "@/components/ui/button";

const examples = [
  "Why did Concorde disappear?",
  "What if Yellowstone erupted tomorrow?",
  "An astronaut wakes alone on a lunar base",
];

export default function Home() {
  const router = useRouter();
  const promptRef = useRef<HTMLTextAreaElement>(null);
  const [prompt, setPrompt] = useState("");
  const [options, setOptions] = useState<Record<string, string | number | null>>({ aspect_ratio: "9:16" });
  const [recent, setRecent] = useState<Project[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    listProjects()
      .then((projects) => { if (active) setRecent(projects.slice(0, 4)); })
      .catch(() => undefined);
    return () => { active = false; };
  }, []);

  async function generate() {
    if (prompt.trim().length < 3 || loading) return;
    setLoading(true);
    setError(null);
    try {
      const created = await createProject(prompt, options);
      try {
        await renderProject(created.id, created.current_revision);
      } catch {
        // The project stays usable and Build readiness explains any missing local provider.
      }
      router.push(`/projects/${created.id}`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not reach the ClipForge API.");
      setLoading(false);
    }
  }

  function newProject() {
    setPrompt("");
    setOptions({ aspect_ratio: "9:16" });
    setError(null);
    requestAnimationFrame(() => promptRef.current?.focus());
  }

  return (
    <main className="relative min-h-screen overflow-hidden">
      <div className="noise" />
      <nav className="mx-auto flex h-20 max-w-[1440px] items-center justify-between px-5 lg:px-10">
        <Brand />
        <div className="flex items-center gap-2">
          <span className="hidden text-sm text-[#77776d] sm:block">Your idea. Fully directed.</span>
          <Button variant="outline" size="sm" onClick={newProject}><Plus className="size-3.5" /> New project</Button>
        </div>
      </nav>

      <section className="mx-auto flex min-h-[calc(100vh-8rem)] max-w-[1020px] flex-col items-center px-5 pb-24 pt-[clamp(3rem,9vh,7rem)] text-center">
        <div className="mb-7 inline-flex items-center gap-2 rounded-full border border-black/8 bg-white/55 px-3.5 py-2 text-[11px] font-bold uppercase tracking-[0.14em] text-[#68685f] shadow-sm backdrop-blur">
          <span className="size-1.5 rounded-full bg-[#ff6838] shadow-[0_0_0_4px_rgba(255,104,56,.12)]" />
          Autonomous shortform studio
        </div>
        <h1 className="balance max-w-[850px] text-[clamp(3rem,7vw,6.7rem)] font-semibold leading-[.91] tracking-[-0.075em]">
          Say what you want <span className="font-normal italic text-[#ff6838]">to know.</span>
        </h1>
        <p className="balance mt-7 max-w-[610px] text-base leading-7 text-[#66665d] md:text-lg">
          ClipForge researches, writes, narrates, renders, and reviews a short you can play and export.
        </p>

        <div className="mt-11 w-full max-w-[780px]">
          <div className="rounded-[30px] border border-black/10 bg-white/85 p-2.5 shadow-[0_26px_80px_rgba(42,38,24,.13),0_2px_8px_rgba(42,38,24,.06)] backdrop-blur-xl transition-[border-color,box-shadow] duration-200 ease-[cubic-bezier(.23,1,.32,1)] focus-within:border-[#ff6838]/30 focus-within:shadow-[0_30px_90px_rgba(42,38,24,.16),0_0_0_4px_rgba(255,104,56,.06)]">
            <textarea
              ref={promptRef}
              autoFocus
              value={prompt}
              onChange={(event) => setPrompt(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) void generate();
              }}
              rows={4}
              placeholder="What should ClipForge create?"
              className="block w-full resize-none bg-transparent px-5 pb-3 pt-4 text-lg leading-7 outline-none placeholder:text-[#a4a49b] md:text-xl"
            />
            <div className="flex items-center justify-between gap-4 border-t border-black/6 px-2 pt-2.5">
              <span className="mono hidden pl-2 text-[10px] uppercase tracking-[.08em] text-[#99998f] sm:inline-flex sm:items-center sm:gap-1.5">
                <CornerDownLeft className="size-3" /> ⌘ Enter
              </span>
              <Button variant="accent" onClick={() => void generate()} disabled={prompt.trim().length < 3 || loading} className="ml-auto min-w-36">
                {loading ? <><LoaderCircle className="size-4 animate-spin" /> Rendering…</> : <>Generate <ArrowRight className="size-4" /></>}
              </Button>
            </div>
          </div>
          {error && <p role="alert" className="mt-3 text-sm font-medium text-red-700">{error} Start the API with <span className="mono">npm run dev</span>.</p>}
          <div className="mt-4"><AdvancedOptions value={options} onChange={setOptions} /></div>
        </div>

        <div className="mt-9 flex flex-wrap justify-center gap-2">
          {examples.map((example) => (
            <button key={example} onClick={() => { setPrompt(example); promptRef.current?.focus(); }} className="rounded-full border border-black/8 bg-white/45 px-4 py-2 text-xs font-medium text-[#69695f] transition-[transform,background-color,border-color,color] duration-150 ease-[cubic-bezier(.23,1,.32,1)] active:scale-[.97] hover:border-black/15 hover:bg-white">
              {example}
            </button>
          ))}
        </div>

        {recent.length > 0 && (
          <div className="mt-14 w-full max-w-[780px] text-left">
            <div className="mb-3 flex items-center gap-2 px-1 text-[11px] font-bold uppercase tracking-[.12em] text-[#85857c]">
              <Clock3 className="size-3.5" /> Recent projects
            </div>
            <div className="grid gap-2 sm:grid-cols-2">
              {recent.map((project) => (
                <Link key={project.id} href={`/projects/${project.id}`} className="group rounded-[18px] border border-black/8 bg-white/50 p-4 transition-[transform,background-color,border-color,box-shadow] duration-150 ease-[cubic-bezier(.23,1,.32,1)] active:scale-[.99] hover:border-black/15 hover:bg-white hover:shadow-sm">
                  <p className="truncate text-sm font-semibold">{project.title}</p>
                  <p className="mono mt-2 text-[9px] uppercase tracking-[.1em] text-[#929289]">v{project.current_revision} · {project.status.replaceAll("_", " ")}</p>
                </Link>
              ))}
            </div>
          </div>
        )}
      </section>
      <div className="pointer-events-none absolute -bottom-24 left-1/2 h-60 w-[70vw] -translate-x-1/2 rounded-[100%] border border-[#ff6838]/10 bg-[#ff9d6f]/8 blur-2xl" />
    </main>
  );
}
