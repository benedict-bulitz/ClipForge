"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { ArrowRight, ChevronDown, Clock3, CornerDownLeft, ListVideo, LoaderCircle, Plus, Settings, Trash2 } from "lucide-react";
import { ApiError, clearGenerationQueue, deleteAllProjects, getBulkProjectDeletePlan, getGenerationJob, getProject, listGenerationJobs, listProjectOverview, removeQueuedGenerationJob, startGeneration } from "@/lib/api";
import { createGenerationWatcher, POLL_TIMEOUT_MS, withTimeout, type GenerationWatcher } from "@/lib/generation-poll";
import type { BulkProjectDeletePlan, GenerationJob, ProjectOverview } from "@/lib/types";
import { activeQueueJobs, visibleProjectHistory } from "@/lib/queue-overview";
import { splitQuestions, submitQuestionsInOrder } from "@/lib/multi-question";
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
export default function Home() {
  const router = useRouter();
  const promptRef = useRef<HTMLTextAreaElement>(null);
  const preferencesReady = useRef(false);
  const generationRequest = useRef(false);
  const [prompt, setPrompt] = useState("");
  const [multipleQuestions, setMultipleQuestions] = useState(false);
  const [options, setOptions] = useState<CreateOptions>({ ...DEFAULT_CREATE_OPTIONS });
  const [recent, setRecent] = useState<ProjectOverview[]>([]);
  const [loading, setLoading] = useState(false);
  const [queue, setQueue] = useState<GenerationJob[]>([]);
  const [queueOpen, setQueueOpen] = useState(false);
  const [queueAction, setQueueAction] = useState<string | null>(null);
  const [recentOpen, setRecentOpen] = useState(false);
  const refreshSequence = useRef(0);
  const mounted = useRef(false);
  const startedWatcher = useRef<GenerationWatcher | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [bulkDeletePlan, setBulkDeletePlan] = useState<BulkProjectDeletePlan | null>(null);
  const [bulkDeleteOpen, setBulkDeleteOpen] = useState(false);
  const [bulkDeletePhrase, setBulkDeletePhrase] = useState("");
  const [bulkDeleting, setBulkDeleting] = useState(false);
  async function refresh() {
    const sequence = ++refreshSequence.current;
    try {
      // Bounded: a hung request must never stop the poll loop on stale progress.
      const [projects, jobs] = await Promise.all([
        withTimeout((signal) => listProjectOverview(signal), POLL_TIMEOUT_MS),
        withTimeout((signal) => listGenerationJobs(signal), POLL_TIMEOUT_MS),
      ]);
      if (!mounted.current || sequence !== refreshSequence.current) return;
      setRecent(projects);
      setQueue(jobs);
    } catch {
      // Keep the last known state until the next refresh.
    }
  }

  useEffect(() => {
    mounted.current = true;
    let timer: number | undefined;
    let stopped = false;
    async function poll() {
      await refresh();
      if (!stopped) timer = window.setTimeout(() => void poll(), 2500);
    }
    void poll();
    return () => {
      stopped = true;
      mounted.current = false;
      if (timer !== undefined) window.clearTimeout(timer);
      startedWatcher.current?.stop();
      startedWatcher.current = null;
    };
  }, []);

  /** Open the project this tab just started as soon as its job completes. */
  function openWhenComplete(job: GenerationJob) {
    startedWatcher.current?.stop();
    const watcher = createGenerationWatcher({
      loadJob: (signal) => getGenerationJob(job.id, signal),
      loadProject: (signal) =>
        getProject(job.project_id, signal).catch((reason: unknown) => {
          if (reason instanceof ApiError && reason.statusCode === 404) return null;
          throw reason;
        }),
      onJob: (next) => {
        if (!mounted.current) return;
        setQueue((items) => items.map((item) => (item.id === next.id ? next : item)));
      },
      onCompleted: (project) => {
        if (!mounted.current || startedWatcher.current !== watcher) return;
        startedWatcher.current = null;
        router.push(`/projects/${project.id}`);
      },
      onFailed: () => {
        if (startedWatcher.current === watcher) startedWatcher.current = null;
      },
    });
    startedWatcher.current = watcher;
    watcher.start();
  }

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
    if (prompt.trim().length < 3 || loading || generationRequest.current) return;
    const submittedText = prompt;
    generationRequest.current = true;
    setLoading(true);
    setError(null);
    try {
      if (!multipleQuestions) {
        const started = await startGeneration(prompt, options);
        if (!started.project_id) throw new Error("The project could not be created.");
        openWhenComplete(started);
        setPrompt("");
        setQueueOpen(true);
        await refresh();
      } else {
        const questions = splitQuestions(submittedText);
        if (questions.length === 0) throw new Error("Enter at least one question.");
        const result = await submitQuestionsInOrder(questions, (question) => startGeneration(question, options));
        if (result.created.length) {
          setQueueOpen(true);
          await refresh();
        }
        if (result.failed) {
          const failed = result.failed;
          setPrompt((current) => current === submittedText ? questions.slice(failed.index).join("\n") : current);
          const detail = failed.reason instanceof Error ? failed.reason.message : "Could not reach the ClipForge API.";
          setError(`Question ${failed.index + 1} could not be queued: "${failed.question}". ${detail}`);
        } else {
          setPrompt((current) => current === submittedText ? "" : current);
        }
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not reach the ClipForge API.");
    } finally {
      generationRequest.current = false;
      setLoading(false);
    }
  }

  async function removeFromQueue(jobId: string) {
    if (queueAction) return;
    setQueueAction(jobId);
    setError(null);
    setQueue((items) => items.filter((item) => item.id !== jobId));
    try {
      await removeQueuedGenerationJob(jobId);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Queued project could not be removed.");
    } finally {
      setQueueAction(null);
      await refresh();
    }
  }

  async function clearQueue() {
    if (queueAction) return;
    setQueueAction("clear");
    setError(null);
    setQueue((items) => items.filter((item) => item.status !== "queued"));
    try {
      await clearGenerationQueue();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Queue could not be cleared.");
    } finally {
      setQueueAction(null);
      await refresh();
    }
  }

  function newProject() {
    setPrompt("");
    setError(null);
    requestAnimationFrame(() => promptRef.current?.focus());
  }

  async function openBulkDelete() {
    setError(null);
    try {
      const plan = await getBulkProjectDeletePlan();
      setBulkDeletePlan(plan);
      setBulkDeletePhrase("");
      setBulkDeleteOpen(plan.project_count > 0);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Projektlöschplan konnte nicht geladen werden.");
    }
  }

  async function confirmBulkDelete() {
    if (bulkDeletePhrase !== "LÖSCHEN" || bulkDeleting) return;
    setBulkDeleting(true);
    setError(null);
    try {
      const result = await deleteAllProjects();
      if (Object.keys(result.failed_projects).length) {
        await refresh();
        setBulkDeleteOpen(false);
        setBulkDeletePlan(null);
        setError("Einige Projekte konnten nicht gelöscht werden. Bitte aktualisieren und erneut prüfen.");
        return;
      }
      setRecent([]);
      setQueue([]);
      setBulkDeleteOpen(false);
      setBulkDeletePlan(null);
      router.replace("/");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Projekte konnten nicht gelöscht werden.");
    } finally {
      setBulkDeleting(false);
    }
  }

  const activeJobs = activeQueueJobs(queue);
  const history = visibleProjectHistory(recent, queue);
  const detectedQuestions = multipleQuestions ? splitQuestions(prompt) : [];

  return (
    <main className="theme-app app-shell relative min-h-screen overflow-hidden">
      <div className="noise" />
      <nav className="mx-auto flex h-[4.5rem] max-w-[1440px] items-center justify-between border-b border-black/5 px-5 lg:px-10">
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

      <section className="mx-auto flex min-h-[calc(100vh-4.5rem)] max-w-[1120px] flex-col items-center px-5 pb-24 pt-[clamp(3.5rem,8vh,6rem)] text-center">
        <div className="cf-surface mb-7 inline-flex items-center gap-2 rounded-full border px-3.5 py-2 text-[11px] font-bold uppercase tracking-[0.14em] text-[var(--muted-foreground)] shadow-sm backdrop-blur">
          <span className="size-1.5 rounded-full bg-[#ff6838] shadow-[0_0_0_4px_rgba(255,104,56,.12)]" />
          Autonomous shortform studio
        </div>
        <h1 className="balance max-w-[780px] text-[clamp(3rem,6.5vw,5.8rem)] font-semibold leading-[.94] tracking-[-0.065em]">
          Say what you want <span className="font-normal italic text-[#ff6838]">to know.</span>
        </h1>
        <p className="balance mt-7 max-w-[610px] text-base leading-7 text-[var(--muted-foreground)] md:text-lg">
          ClipForge researches, writes, narrates, renders, and reviews a short you can play and export.
        </p>

        <div className="mt-11 w-full max-w-[780px]">
          <div className="creation-composer cf-surface border p-2.5 transition-[border-color,box-shadow] duration-200 ease-[cubic-bezier(.23,1,.32,1)] focus-within:border-[#ff6838]/30 focus-within:shadow-[0_22px_70px_rgba(42,38,24,.12),0_0_0_4px_rgba(255,104,56,.06)]">
            <textarea
              ref={promptRef}
              autoFocus
              value={prompt}
              onChange={(event) => setPrompt(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) void generate();
              }}
              rows={4}
              aria-label="Describe the video to create"
              placeholder="What should ClipForge create?"
              className="block w-full resize-none bg-transparent px-5 pb-3 pt-4 text-lg leading-7 text-[var(--foreground)] outline-none placeholder:text-[var(--muted-foreground)] md:text-xl"
            />
            <div className="flex items-center justify-between gap-4 border-t border-black/6 px-2 pt-2.5">
              <span className="mono hidden pl-2 text-[10px] uppercase tracking-[.08em] text-[#99998f] sm:inline-flex sm:items-center sm:gap-1.5">
                <CornerDownLeft className="size-3" /> ⌘ Enter
              </span>
              <Button variant="accent" onClick={() => void generate()} disabled={prompt.trim().length < 3 || loading} className="ml-auto min-w-36">
                {loading ? <><LoaderCircle className="size-4 animate-spin" /> Starting…</> : <>Generate <ArrowRight className="size-4" /></>}
              </Button>
            </div>
          </div>
          <label className="mt-3 flex items-center gap-2 px-2 text-left text-xs font-medium text-[var(--muted-foreground)]">
            <input type="checkbox" checked={multipleQuestions} onChange={(event) => setMultipleQuestions(event.target.checked)} disabled={loading} className="size-4 accent-[var(--accent)]" />
            Multiple questions
          </label>
          {multipleQuestions && prompt.trim() && <p className="mt-1 px-2 text-left text-xs text-[var(--muted-foreground)]">{detectedQuestions.length} question{detectedQuestions.length === 1 ? "" : "s"} detected</p>}
          {error && <p role="alert" className="mt-3 text-sm font-medium text-[var(--destructive)]">{error}</p>}
          <div className="mt-3"><AdvancedOptions value={options} onChange={setOptions} prompt={prompt} onReset={() => setOptions(resetCreatePreferences())} queueToggle={<button type="button" aria-expanded={queueOpen} aria-controls="video-queue-panel" onClick={() => setQueueOpen((open) => !open)} className="flex items-center gap-2 rounded-full px-3 py-2 text-sm font-medium text-[var(--muted-foreground)] hover:bg-[var(--surface-hover)] hover:text-[var(--foreground)]"><ListVideo className="size-4" /> Video Queue <span className="rounded-full bg-[var(--accent-soft)] px-2 py-0.5 text-xs font-bold text-[var(--accent)]">{activeJobs.length}</span><ChevronDown className={`size-4 transition-transform ${queueOpen ? "rotate-180" : ""}`} /></button>} /></div>
        </div>

        {queueOpen && <GenerationQueue jobs={activeJobs} onRemove={removeFromQueue} onClear={clearQueue} busy={queueAction !== null} />}

        <div className="mt-9 flex flex-wrap justify-center gap-2">
          {examples.map((example) => (
            <button key={example} onClick={() => { setPrompt(example); promptRef.current?.focus(); }} className="cf-surface rounded-full border px-4 py-2 text-xs font-medium text-[var(--muted-foreground)] transition-[transform,background-color,border-color,color] duration-150 ease-[cubic-bezier(.23,1,.32,1)] active:scale-[.97] hover:bg-[var(--surface-hover)] hover:text-[var(--foreground)]">
              {example}
            </button>
          ))}
        </div>

        {recent.length > 0 && (
          <div className="mt-14 w-full max-w-[780px] text-left">
            <div className="mb-3 flex items-center justify-between gap-2 px-1 text-[11px] font-bold uppercase tracking-[.12em] text-[#85857c]">
              <button type="button" aria-expanded={recentOpen} aria-controls="recent-projects-panel" onClick={() => setRecentOpen((open) => !open)} className="flex items-center gap-2"><Clock3 className="size-3.5" /> Recent Projects ({history.length}) <ChevronDown className={`size-3.5 transition-transform ${recentOpen ? "rotate-180" : ""}`} /></button>
              <button onClick={() => void openBulkDelete()} className="flex items-center gap-1 text-red-700 hover:text-red-800"><Trash2 className="size-3.5" /> Alle Projekte löschen</button>
            </div>
            {recentOpen && <div id="recent-projects-panel" className="grid max-h-[38rem] gap-2 overflow-y-auto sm:grid-cols-2">
              {history.map((project) => (
                <Link key={project.id} href={`/projects/${project.id}`} className="cf-surface group rounded-[18px] border p-4 transition-[transform,background-color,border-color,box-shadow] duration-150 ease-[cubic-bezier(.23,1,.32,1)] active:scale-[.99] hover:bg-[var(--surface-hover)] hover:shadow-sm">
                  <p className="truncate text-sm font-semibold">{project.title}</p>
                  <p className="mono mt-2 text-[9px] uppercase tracking-[.1em] text-[#929289]">{project.current_revision ? `v${project.current_revision} · ` : ""}{project.status.replaceAll("_", " ")}</p>
                </Link>
              ))}
            </div>}
          </div>
        )}
      </section>
      {bulkDeleteOpen && bulkDeletePlan && (
        <div className="fixed inset-0 z-50 grid place-items-center bg-black/45 p-4" role="dialog" aria-modal="true" aria-labelledby="bulk-delete-title">
          <div className="cf-surface w-full max-w-md rounded-[24px] border p-6 text-left shadow-[0_22px_70px_rgba(0,0,0,.25)]">
            <h2 id="bulk-delete-title" className="text-lg font-semibold">Alle Projekte wirklich löschen?</h2>
            <p className="mt-2 text-sm leading-6 text-[var(--muted-foreground)]">Alle projektlokalen Videos, Audio-Dateien, Medienableitungen und Projektdaten werden dauerhaft gelöscht. Wiederverwendbare Caches bleiben erhalten.</p>
            <p className="mt-4 rounded-xl bg-black/[.04] px-3 py-2 text-sm font-semibold">{bulkDeletePlan.project_count} Projekte · ca. {formatBytes(bulkDeletePlan.total_bytes)}</p>
            <label className="mt-5 block text-sm font-medium">Zum Bestätigen <span className="font-bold">LÖSCHEN</span> eingeben
              <input aria-label="Type LÖSCHEN to confirm deletion" value={bulkDeletePhrase} onChange={(event) => setBulkDeletePhrase(event.target.value)} className="mt-2 w-full rounded-xl border bg-transparent px-3 py-2 outline-none focus:border-[#ff6838]" autoComplete="off" />
            </label>
            <div className="mt-6 flex justify-end gap-2">
              <Button variant="ghost" disabled={bulkDeleting} onClick={() => setBulkDeleteOpen(false)}>Abbrechen</Button>
              <Button variant="accent" disabled={bulkDeletePhrase !== "LÖSCHEN" || bulkDeleting} onClick={() => void confirmBulkDelete()}>
                {bulkDeleting ? <LoaderCircle className="size-3.5 animate-spin" /> : <Trash2 className="size-3.5" />} Alle Projekte löschen
              </Button>
            </div>
          </div>
        </div>
      )}
      <div className="pointer-events-none absolute -bottom-24 left-1/2 h-60 w-[70vw] -translate-x-1/2 rounded-[100%] border border-[#ff6838]/10 bg-[#ff9d6f]/8 blur-2xl" />
    </main>
  );
}

function formatBytes(bytes: number) {
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GB`;
}

function GenerationQueue({ jobs, onRemove, onClear, busy }: { jobs: GenerationJob[]; onRemove: (jobId: string) => void; onClear: () => void; busy: boolean }) {
  const running = jobs.filter((job) => job.status === "running");
  const waiting = jobs.filter((job) => job.status === "queued");
  const [clearConfirmationOpen, setClearConfirmationOpen] = useState(false);
  return (
    <section id="video-queue-panel" className="queue-card cf-surface mt-5 w-full max-w-[780px] border p-4 text-left" aria-label="Video Queue" aria-live="polite">
      {jobs.length === 0 && <p className="text-sm text-[var(--muted-foreground)]">Keine Videos in der Warteschlange.</p>}
      {running.map((item) => <Link key={item.project_id} href={`/projects/${item.project_id}`} className="block rounded-xl border border-[#ff6838]/30 bg-[#ff6838]/5 p-4 hover:bg-[#ff6838]/10">
        <p className="flex items-center justify-between text-xs font-bold text-[#d94c20]"><span>● Wird erstellt</span><span>{Math.round(item.progress * 100)}%</span></p>
        <p className="mt-2 break-words text-sm font-semibold">{item.prompt}</p>
        <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-[#ff6838]/15" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(item.progress * 100)}><div className="h-full bg-[#ff6838]" style={{ width: `${Math.round(item.progress * 100)}%` }} /></div>
      </Link>)}
      {waiting.length > 0 && <div className="mb-2 mt-4 flex items-center justify-between gap-3"><p className="text-[11px] font-bold uppercase tracking-[.12em] text-[#85857c]">Warteschlange</p><button type="button" disabled={busy} onClick={() => setClearConfirmationOpen(true)} className="text-xs font-semibold text-red-700 hover:text-red-800 disabled:opacity-50">Clear Queue</button></div>}
      {clearConfirmationOpen && <div className="mb-3 flex items-center justify-between gap-3 rounded-xl border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-900"><span>Clear {waiting.length} queued project{waiting.length === 1 ? "" : "s"}?</span><span className="flex gap-2"><button type="button" disabled={busy} onClick={() => setClearConfirmationOpen(false)}>Cancel</button><button type="button" disabled={busy} onClick={() => { setClearConfirmationOpen(false); onClear(); }} className="font-bold text-red-800">Clear Queue</button></span></div>}
      <div className="grid gap-2">{waiting.map((item) => <div key={item.project_id} className="flex items-start gap-3 rounded-xl border p-3 hover:bg-[var(--surface-hover)]"><Link href={`/projects/${item.project_id}`} className="flex min-w-0 flex-1 items-start gap-3"><span className="mono shrink-0 text-xs font-bold text-[#d94c20]">#{item.queue_position}</span><span className="min-w-0"><span className="block break-words text-sm font-semibold">{item.prompt}</span><span className="mt-1 block text-xs text-[var(--muted-foreground)]">In Warteschlange</span></span></Link><button type="button" aria-label={`Remove ${item.prompt} from queue`} disabled={busy} onClick={() => onRemove(item.id)} className="shrink-0 text-xs font-semibold text-red-700 hover:text-red-800 disabled:opacity-50">Remove</button></div>)}</div>
    </section>
  );
}
