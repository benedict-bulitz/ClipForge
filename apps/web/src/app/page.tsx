"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { ArrowRight, Clock3, CornerDownLeft, LoaderCircle, Plus, Settings } from "lucide-react";
import { getActiveGenerationJob, getGenerationJob, listProjects, startGeneration } from "@/lib/api";
import type { GenerationJob, Project } from "@/lib/types";
import { AdvancedOptions } from "@/components/advanced-options";
import { Brand } from "@/components/brand";
import { Button } from "@/components/ui/button";
import { ThemeToggle } from "@/components/theme-toggle";
import {
  DEFAULT_CREATE_OPTIONS,
  loadCreatePreferences,
  resetCreatePreferences,
  saveCreatePreferences,
  type CreateOptions,
} from "@/lib/creation-preferences";

const examples = [
  "Why did Concorde disappear?",
  "What if Yellowstone erupted tomorrow?",
  "An astronaut wakes alone on a lunar base",
];
const ACTIVE_JOB_KEY = "clipforge.active-generation-job";

export default function Home() {
  const router = useRouter();
  const promptRef = useRef<HTMLTextAreaElement>(null);
  const preferencesReady = useRef(false);
  const generationRequest = useRef(false);
  const [prompt, setPrompt] = useState("");
  const [options, setOptions] = useState<CreateOptions>({ ...DEFAULT_CREATE_OPTIONS });
  const [recent, setRecent] = useState<Project[]>([]);
  const [loading, setLoading] = useState(false);
  const [job, setJob] = useState<GenerationJob | null>(null);
  const [error, setError] = useState<string | null>(null);
  const activeJobId = job?.id;
  const activeJobStatus = job?.status;

  useEffect(() => {
    let active = true;
    listProjects()
      .then((projects) => { if (active) setRecent(projects.slice(0, 4)); })
      .catch(() => undefined);
    return () => { active = false; };
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    const saved = window.localStorage.getItem(ACTIVE_JOB_KEY);
    const recover = saved
      ? getGenerationJob(saved, controller.signal).catch(() => null)
      : getActiveGenerationJob(controller.signal);
    recover.then((found) => {
      if (!found) return;
      setJob(found);
      window.localStorage.setItem(ACTIVE_JOB_KEY, found.id);
      if (found.status === "completed") {
        window.localStorage.removeItem(ACTIVE_JOB_KEY);
        router.replace(`/projects/${found.project_id}`);
      }
    }).catch(() => undefined);
    return () => controller.abort();
  }, [router]);

  useEffect(() => {
    if (!activeJobId || !activeJobStatus || !["queued", "running"].includes(activeJobStatus)) return;
    const jobId = activeJobId;
    let stopped = false;
    let timer: number | null = null;
    let controller: AbortController | null = null;
    async function poll() {
      controller = new AbortController();
      try {
        const next = await getGenerationJob(activeJobId!, controller.signal);
        if (stopped) return;
        setJob(next);
        if (next.status === "completed") {
          window.localStorage.removeItem(ACTIVE_JOB_KEY);
          router.push(`/projects/${next.project_id}`);
          return;
        }
        if (next.status === "failed") return;
      } catch (reason) {
        if (stopped || controller?.signal.aborted) return;
        setError(reason instanceof Error ? reason.message : "Generation progress could not be refreshed.");
      }
      if (!stopped) timer = window.setTimeout(poll, 750);
    }
    timer = window.setTimeout(poll, 400);
    return () => {
      stopped = true;
      if (timer) window.clearTimeout(timer);
      controller?.abort();
    };
  }, [activeJobId, activeJobStatus, router]);

  useEffect(() => {
    const frame = requestAnimationFrame(() => {
      setOptions(loadCreatePreferences());
      preferencesReady.current = true;
    });
    return () => cancelAnimationFrame(frame);
  }, []);

  useEffect(() => {
    if (preferencesReady.current) saveCreatePreferences(options);
  }, [options]);

  async function generate() {
    if (prompt.trim().length < 3 || loading || jobActive || generationRequest.current) return;
    generationRequest.current = true;
    setLoading(true);
    setError(null);
    try {
      const started = await startGeneration(prompt, options);
      setJob(started);
      window.localStorage.setItem(ACTIVE_JOB_KEY, started.id);
      if (started.status === "completed") {
        window.localStorage.removeItem(ACTIVE_JOB_KEY);
        router.push(`/projects/${started.project_id}`);
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not reach the ClipForge API.");
    } finally {
      generationRequest.current = false;
      setLoading(false);
    }
  }

  function newProject() {
    setPrompt("");
    setError(null);
    setJob(null);
    window.localStorage.removeItem(ACTIVE_JOB_KEY);
    requestAnimationFrame(() => promptRef.current?.focus());
  }

  const jobActive = job?.status === "queued" || job?.status === "running";

  return (
    <main className="theme-app relative min-h-screen overflow-hidden">
      <div className="noise" />
      <nav className="mx-auto flex h-20 max-w-[1440px] items-center justify-between px-5 lg:px-10">
        <Brand />
        <div className="flex items-center gap-2">
          <span className="hidden text-sm text-[#77776d] sm:block">Your idea. Fully directed.</span>
          <ThemeToggle />
          <Button asChild variant="ghost" size="sm">
            <Link href="/settings/integrations"><Settings className="size-3.5" /> <span className="hidden sm:inline">Settings</span></Link>
          </Button>
          <Button variant="outline" size="sm" onClick={newProject}><Plus className="size-3.5" /> New project</Button>
        </div>
      </nav>

      <section className="mx-auto flex min-h-[calc(100vh-8rem)] max-w-[1020px] flex-col items-center px-5 pb-24 pt-[clamp(3rem,9vh,7rem)] text-center">
        <div className="cf-surface mb-7 inline-flex items-center gap-2 rounded-full border px-3.5 py-2 text-[11px] font-bold uppercase tracking-[0.14em] text-[var(--muted-foreground)] shadow-sm backdrop-blur">
          <span className="size-1.5 rounded-full bg-[#ff6838] shadow-[0_0_0_4px_rgba(255,104,56,.12)]" />
          Autonomous shortform studio
        </div>
        <h1 className="balance max-w-[850px] text-[clamp(3rem,7vw,6.7rem)] font-semibold leading-[.91] tracking-[-0.075em]">
          Say what you want <span className="font-normal italic text-[#ff6838]">to know.</span>
        </h1>
        <p className="balance mt-7 max-w-[610px] text-base leading-7 text-[var(--muted-foreground)] md:text-lg">
          ClipForge researches, writes, narrates, renders, and reviews a short you can play and export.
        </p>

        <div className="mt-11 w-full max-w-[780px]">
          <div className="cf-surface rounded-[30px] border p-2.5 shadow-[0_26px_80px_rgba(42,38,24,.13),0_2px_8px_rgba(42,38,24,.06)] backdrop-blur-xl transition-[border-color,box-shadow] duration-200 ease-[cubic-bezier(.23,1,.32,1)] focus-within:border-[#ff6838]/30 focus-within:shadow-[0_30px_90px_rgba(42,38,24,.16),0_0_0_4px_rgba(255,104,56,.06)]">
            <textarea
              ref={promptRef}
              autoFocus
              value={prompt}
              disabled={jobActive}
              onChange={(event) => setPrompt(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) void generate();
              }}
              rows={4}
              placeholder="What should ClipForge create?"
              className="block w-full resize-none bg-transparent px-5 pb-3 pt-4 text-lg leading-7 text-[var(--foreground)] outline-none placeholder:text-[var(--muted-foreground)] md:text-xl"
            />
            <div className="flex items-center justify-between gap-4 border-t border-black/6 px-2 pt-2.5">
              <span className="mono hidden pl-2 text-[10px] uppercase tracking-[.08em] text-[#99998f] sm:inline-flex sm:items-center sm:gap-1.5">
                <CornerDownLeft className="size-3" /> ⌘ Enter
              </span>
              <Button variant="accent" onClick={() => void generate()} disabled={prompt.trim().length < 3 || loading || jobActive} className="ml-auto min-w-36">
                {loading || jobActive ? <><LoaderCircle className="size-4 animate-spin" /> {loading ? "Starting…" : "Generating…"}</> : <>Generate <ArrowRight className="size-4" /></>}
              </Button>
            </div>
          </div>
          {job && <GenerationProgressCard job={job} onRetry={() => { setJob(null); window.localStorage.removeItem(ACTIVE_JOB_KEY); void generate(); }} />}
          {error && <p role="alert" className="mt-3 text-sm font-medium text-[var(--destructive)]">{error}</p>}
          {!jobActive && <div className="mt-3"><AdvancedOptions value={options} onChange={setOptions} prompt={prompt} onReset={() => setOptions(resetCreatePreferences())} /></div>}
        </div>

        <div className="mt-9 flex flex-wrap justify-center gap-2">
          {examples.map((example) => (
            <button key={example} onClick={() => { setPrompt(example); promptRef.current?.focus(); }} className="cf-surface rounded-full border px-4 py-2 text-xs font-medium text-[var(--muted-foreground)] transition-[transform,background-color,border-color,color] duration-150 ease-[cubic-bezier(.23,1,.32,1)] active:scale-[.97] hover:bg-[var(--surface-hover)] hover:text-[var(--foreground)]">
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
                <Link key={project.id} href={`/projects/${project.id}`} className="cf-surface group rounded-[18px] border p-4 transition-[transform,background-color,border-color,box-shadow] duration-150 ease-[cubic-bezier(.23,1,.32,1)] active:scale-[.99] hover:bg-[var(--surface-hover)] hover:shadow-sm">
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

function GenerationProgressCard({ job, onRetry }: { job: GenerationJob; onRetry: () => void }) {
  const percent = job.status === "completed" ? 100 : Math.min(99, Math.round(job.progress * 100));
  const units = job.total_units
    ? ` · ${job.current_stage === "media" || job.current_stage === "rendering" ? "Scene " : ""}${job.completed_units ?? 0} of ${job.total_units}`
    : "";
  return (
    <section className="cf-surface mt-4 rounded-[22px] border p-4 text-left shadow-sm" aria-live="polite" aria-label="Video generation progress">
      <div className="flex items-center justify-between gap-4">
        <div>
          <p className="text-sm font-bold">{job.status === "failed" ? "Generation stopped" : job.status === "completed" ? "Video complete" : "Generating your video"}</p>
          <p className="mt-1 text-xs text-[var(--muted-foreground)]">{job.stage_label}{units}</p>
        </div>
        <span className="text-2xl font-semibold tracking-[-.04em]">{percent}%</span>
      </div>
      <div className="mt-3 h-2 overflow-hidden rounded-full bg-[var(--surface-subtle)]" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent}>
        <div className="h-full rounded-full bg-[var(--accent)] transition-[width] duration-300" style={{ width: `${percent}%` }} />
      </div>
      <div className="mt-2 flex items-center justify-between gap-3 text-[11px] text-[var(--muted-foreground)]">
        <span>{formatEta(job)}</span>
        {job.elapsed_seconds >= 10 && <span>{formatElapsed(job.elapsed_seconds)} elapsed</span>}
      </div>
      {job.status === "failed" && (
        <div className="mt-3 flex items-center justify-between gap-3 rounded-xl bg-red-50 p-3 text-xs text-red-800">
          <span>{job.failure_message ?? "Generation could not be completed. Your previous project state is safe."}</span>
          <Button size="sm" variant="outline" onClick={onRetry}>Retry</Button>
        </div>
      )}
    </section>
  );
}

function formatEta(job: GenerationJob) {
  if (job.status === "completed") return "Complete";
  if (job.status === "failed") return "Ready to retry";
  const seconds = job.estimated_remaining_seconds;
  if (seconds === null) return "Estimating…";
  if (seconds < 10) return "Less than 10 sec remaining";
  if (seconds < 60) return `About ${Math.round(seconds / 5) * 5} sec remaining`;
  const minutes = Math.floor(seconds / 60);
  const remainder = Math.round((seconds % 60) / 10) * 10;
  return `About ${minutes} min${remainder ? ` ${remainder} sec` : ""} remaining`;
}

function formatElapsed(seconds: number) {
  if (seconds < 60) return `${Math.floor(seconds)} sec`;
  return `${Math.floor(seconds / 60)} min ${Math.floor(seconds % 60)} sec`;
}
