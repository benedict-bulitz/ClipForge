"use client";

import { useEffect, useRef, useState } from "react";
import { ArrowRight, ChevronDown, Clapperboard, Clock3, CornerDownLeft, ListVideo, LoaderCircle, Plus, RefreshCw, Settings, Sparkles, Trash2 } from "lucide-react";
import { ApiError, cancelGenerationJob, clearGenerationQueue, deleteAllProjects, getBulkProjectDeletePlan, getGenerationJob, getProject, listGenerationJobs, listProjectOverview, loadTopicSuggestions, removeQueuedGenerationJob, selectAutoTopic, startGeneration } from "@/lib/api";
import { createGenerationWatcher, generationTimeLabel, POLL_TIMEOUT_MS, withTimeout, type GenerationWatcher } from "@/lib/generation-poll";
import type { BulkProjectDeletePlan, GenerationJob, ProjectOverview } from "@/lib/types";
import { activeQueueJobs, deletableProjectCount, historyStatusLabel, queueDisplayJobs, visibleProjectHistory } from "@/lib/queue-overview";
import { applyJob, CANCEL_LABEL, CANCELLED_LABEL, CANCELLING_LABEL, cancelView, createCancelRequester, markCancelling, type CancelRequester } from "@/lib/generation-cancel";
import { createHomePoller, type HomePoller } from "@/lib/home-poll";
import { splitQuestions, submitQuestionsInOrder } from "@/lib/multi-question";
import { autoTopicOutcome, browserSuggestionStorage, createTopicSuggestions, emptySuggestions, topicGenerationSource, type SuggestionState, type TopicSuggestion, type TopicSuggestions } from "@/lib/topic-suggestions";
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
import { PageLink } from "@/components/page-link";

/** Discovery may run (once) behind a chip refill; it never blocks anything else. */
const SUGGESTION_TIMEOUT_MS = 90_000;

export default function Home() {
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
  // Running jobs whose cancel request is on its way, and jobs cancelled from this page.
  const [cancelPending, setCancelPending] = useState<ReadonlySet<string>>(new Set());
  const [cancelledHere, setCancelledHere] = useState<ReadonlySet<string>>(new Set());
  const cancelRequester = useRef<CancelRequester | null>(null);
  const [projectsLoaded, setProjectsLoaded] = useState(false);
  const poller = useRef<HomePoller | null>(null);
  const mounted = useRef(false);
  const startedWatcher = useRef<GenerationWatcher | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [bulkDeletePlan, setBulkDeletePlan] = useState<BulkProjectDeletePlan | null>(null);
  const [bulkDeleteOpen, setBulkDeleteOpen] = useState(false);
  const [bulkDeletePhrase, setBulkDeletePhrase] = useState("");
  const [bulkDeleting, setBulkDeleting] = useState(false);
  const [suggestions, setSuggestions] = useState<SuggestionState>(emptySuggestions);
  /** The chip the textarea came from (provenance for the existing Generate button). */
  const [chosenTopic, setChosenTopic] = useState<TopicSuggestion | null>(null);
  const suggestionsRef = useRef<TopicSuggestions | null>(null);
  /** Full Auto: which question was chosen automatically, or why none was (shown under the composer). */
  const [autoNote, setAutoNote] = useState<string | null>(null);
  /** Jobs + history now; the poller then keeps the right cadence (see home-poll.ts). */
  async function refresh() {
    await poller.current?.refreshAll();
  }

  useEffect(() => {
    mounted.current = true;
    // The one polling authority for this page. Strict Mode's mount/unmount/mount
    // stops the first instance before the second starts: one timer at a time.
    const instance = createHomePoller({
      // Bounded: a hung request must never stop the cadence on stale progress.
      loadJobs: () => withTimeout((signal) => listGenerationJobs(signal), POLL_TIMEOUT_MS),
      loadProjects: () => withTimeout((signal) => listProjectOverview(signal), POLL_TIMEOUT_MS),
      onJobs: (jobs) => { if (mounted.current) setQueue(jobs); },
      onProjects: (projects) => {
        if (!mounted.current) return;
        setRecent(projects);
        setProjectsLoaded(true);
      },
    });
    poller.current = instance;
    instance.start();
    return () => {
      instance.stop();
      if (poller.current === instance) poller.current = null;
      mounted.current = false;
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
        // A real page: a document navigation, so Back returns here (Safari skips entries
        // that Next.js creates with pushState, so router.push is deliberately not used).
        // eslint-disable-next-line @next/next/no-location-assign-relative-destination
        window.location.assign(`/projects/${project.id}`);
      },
      onFailed: () => {
        if (startedWatcher.current === watcher) startedWatcher.current = null;
      },
    });
    startedWatcher.current = watcher;
    watcher.start();
  }

  useEffect(() => {
    // Live Topic Intelligence chips: the persisted visible set is shown as-is;
    // only the hidden reserve (and empty slots) are refilled in the background.
    const controller = createTopicSuggestions({
      load: (request) => withTimeout((signal) => loadTopicSuggestions(request, signal), SUGGESTION_TIMEOUT_MS),
      onChange: (next) => { if (mounted.current) setSuggestions(next); },
      storage: browserSuggestionStorage(),
    });
    suggestionsRef.current = controller;
    controller.start();
    return () => {
      controller.stop();
      if (suggestionsRef.current === controller) suggestionsRef.current = null;
    };
  }, []);

  /** Chip click: fill the textarea only; the slot is refilled instantly from the reserve. */
  function applySuggestion(index: number) {
    const picked = suggestionsRef.current?.pick(index);
    if (!picked) return;
    setPrompt(picked.question);
    setChosenTopic(picked);
    setError(null);
    requestAnimationFrame(() => promptRef.current?.focus());
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
        // A question taken from a suggestion chip keeps its Topic Intelligence provenance.
        const topic = topicGenerationSource(chosenTopic, prompt);
        const started = topic ? await startGeneration(prompt, options, topic) : await startGeneration(prompt, options);
        if (!started.project_id) throw new Error("The project could not be created.");
        openWhenComplete(started);
        setPrompt("");
        setChosenTopic(null);
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

  /**
   * "Generate automatically" (Full Auto): Topic Intelligence picks the strongest eligible
   * question; it is started through the same generation entry point with its provenance.
   * Nothing starts when no question meets the minimum quality.  The textarea is untouched.
   */
  async function generateAutomatically() {
    if (loading || generationRequest.current) return;
    generationRequest.current = true;
    setLoading(true);
    setError(null);
    setAutoNote("Die stärkste nächste Frage wird gesucht…");
    try {
      const outcome = autoTopicOutcome(await withTimeout((signal) => selectAutoTopic(signal), SUGGESTION_TIMEOUT_MS));
      if (outcome.kind !== "selected") {
        setAutoNote(outcome.message);
        return;
      }
      const started = await startGeneration(outcome.question, options, outcome.source);
      if (!started.project_id) throw new Error("The project could not be created.");
      setAutoNote(`Automatisch gewählt: „${outcome.question}“${outcome.signalLabel ? ` · ${outcome.signalLabel}` : ""}${outcome.reason ? ` – ${outcome.reason}` : ""}`);
      openWhenComplete(started);
      setQueueOpen(true);
      await refresh();
    } catch (reason) {
      setAutoNote(null);
      setError(reason instanceof Error ? reason.message : "Could not reach the ClipForge API.");
    } finally {
      generationRequest.current = false;
      setLoading(false);
    }
  }

  /** "Abbrechen": the card says "Wird abgebrochen…" at once; one request per job, however often clicked. */
  function cancelRunning(jobId: string) {
    cancelRequester.current ??= createCancelRequester({
      cancel: cancelGenerationJob,
      onPending: (pending) => { if (mounted.current) setCancelPending(pending); },
      onJob: (job) => {
        if (!mounted.current) return;
        setQueue((items) => applyJob(items, job));
        setCancelledHere((ids) => new Set([...ids, job.id]));
        void refresh();
      },
      onError: (message) => { if (mounted.current) setError(message); },
    });
    setError(null);
    if (cancelRequester.current.request(jobId)) setQueue((items) => markCancelling(items, jobId));
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
    setChosenTopic(null);
    setError(null);
    requestAnimationFrame(() => promptRef.current?.focus());
  }

  async function openBulkDelete() {
    setError(null);
    try {
      const plan = await getBulkProjectDeletePlan();
      setBulkDeletePlan(plan);
      setBulkDeletePhrase("");
      setBulkDeleteOpen(plan.project_count + (plan.history_entry_count ?? 0) > 0);
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
      // What remains (e.g. early-failed requests without a project) comes from the API.
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Projekte konnten nicht gelöscht werden.");
    } finally {
      setBulkDeleting(false);
    }
  }

  const activeJobs = activeQueueJobs(queue);
  const queueJobs = queueDisplayJobs(queue, cancelledHere);
  const history = visibleProjectHistory(recent, queue);
  const deletableProjects = deletableProjectCount(history);
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
            <PageLink href="/videos"><Clapperboard className="size-3.5" /> <span className="hidden sm:inline">Videos</span></PageLink>
          </Button>
          <Button asChild variant="ghost" size="sm">
            <PageLink href="/settings/integrations"><Settings className="size-3.5" /> <span className="hidden sm:inline">Settings</span></PageLink>
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
              onChange={(event) => {
                setPrompt(event.target.value);
                if (!event.target.value.trim()) setChosenTopic(null);
              }}
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
              <Button variant="outline" onClick={() => void generateAutomatically()} disabled={loading} className="ml-auto" title="ClipForge wählt die stärkste nächste Frage selbst – oder sagt, dass gerade keine stark genug ist.">
                <Sparkles className="size-4" /> Generate automatically
              </Button>
              <Button variant="accent" onClick={() => void generate()} disabled={prompt.trim().length < 3 || loading} className="min-w-36">
                {loading ? <><LoaderCircle className="size-4 animate-spin" /> Starting…</> : <>Generate <ArrowRight className="size-4" /></>}
              </Button>
            </div>
          </div>
          <label className="mt-3 flex items-center gap-2 px-2 text-left text-xs font-medium text-[var(--muted-foreground)]">
            <input type="checkbox" checked={multipleQuestions} onChange={(event) => setMultipleQuestions(event.target.checked)} disabled={loading} className="size-4 accent-[var(--accent)]" />
            Multiple questions
          </label>
          {multipleQuestions && prompt.trim() && <p className="mt-1 px-2 text-left text-xs text-[var(--muted-foreground)]">{detectedQuestions.length} question{detectedQuestions.length === 1 ? "" : "s"} detected</p>}
          {autoNote && <p role="status" aria-live="polite" className="mt-2 px-2 text-left text-xs text-[var(--muted-foreground)]">{autoNote}</p>}
          {error && <p role="alert" className="cf-text-error mt-3 text-sm font-medium">{error}</p>}
          <div className="mt-3"><AdvancedOptions value={options} onChange={setOptions} prompt={prompt} onReset={() => setOptions(resetCreatePreferences())} queueToggle={<button type="button" aria-expanded={queueOpen} aria-controls="video-queue-panel" onClick={() => setQueueOpen((open) => !open)} className="flex items-center gap-2 rounded-full px-3 py-2 text-sm font-medium text-[var(--muted-foreground)] hover:bg-[var(--surface-hover)] hover:text-[var(--foreground)]"><ListVideo className="size-4" /> Video Queue <span className="rounded-full bg-[var(--accent-soft)] px-2 py-0.5 text-xs font-bold text-[var(--accent)]">{activeJobs.length}</span><ChevronDown className={`size-4 transition-transform ${queueOpen ? "rotate-180" : ""}`} /></button>} /></div>
        </div>

        {queueOpen && <GenerationQueue jobs={queueJobs} onRemove={removeFromQueue} onClear={clearQueue} onCancel={cancelRunning} cancelPending={cancelPending} busy={queueAction !== null} />}

        <TopicSuggestionChips state={suggestions} onUse={applySuggestion} onRefreshAll={() => suggestionsRef.current?.refreshAll()} />

        <section className="mt-14 w-full max-w-[780px] text-left" aria-labelledby="recent-projects-title">
          {/* text-[11px] on the row: a global `button { font: inherit }` rule overrides the button's own size. */}
          <div className="mb-3 flex min-h-8 items-center justify-between gap-2 px-1 text-[11px]">
            <h2 id="recent-projects-title" className="flex items-center gap-2 text-[11px] font-bold uppercase tracking-[.12em] text-[#85857c]"><Clock3 className="size-3.5" /> Recent Projects{history.length > 0 ? ` (${history.length})` : ""}</h2>
            {deletableProjects > 0 && (
              <button type="button" onClick={() => void openBulkDelete()} className="inline-flex items-center gap-1.5 rounded-full border border-transparent px-2.5 py-1 text-[11px] font-semibold text-[var(--muted-foreground)] transition-colors duration-150 hover:border-red-200 hover:bg-red-50 hover:text-red-700 focus-visible:border-red-300 focus-visible:text-red-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-red-500/30 dark:hover:border-red-500/30 dark:hover:bg-red-500/10 dark:hover:text-red-300">
                <Trash2 className="size-3.5" /> Alle Projekte löschen
              </button>
            )}
          </div>
          {history.length > 0 ? (
            <div id="recent-projects-list" className="grid gap-2 sm:grid-cols-2">
              {history.map((project) => (
                <PageLink key={project.id} href={`/projects/${project.id}`} className="cf-surface group rounded-[18px] border p-4 transition-[transform,background-color,border-color,box-shadow] duration-150 ease-[cubic-bezier(.23,1,.32,1)] active:scale-[.99] hover:bg-[var(--surface-hover)] hover:shadow-sm">
                  <p className="truncate text-sm font-semibold">{project.title}</p>
                  <p className="mono mt-2 text-[9px] uppercase tracking-[.1em] text-[#929289]">{project.current_revision ? `v${project.current_revision} · ` : ""}{historyStatusLabel(project.status)}</p>
                </PageLink>
              ))}
            </div>
          ) : projectsLoaded ? (
            <div className="px-1 py-2">
              <p className="text-sm font-semibold">Noch keine Projekte</p>
              <p className="mt-0.5 text-xs text-[var(--muted-foreground)]">Deine erstellten Videos erscheinen hier.</p>
            </div>
          ) : null}
        </section>
      </section>
      {bulkDeleteOpen && bulkDeletePlan && (
        <div className="fixed inset-0 z-50 grid place-items-center bg-black/45 p-4" role="dialog" aria-modal="true" aria-labelledby="bulk-delete-title">
          <div className="cf-surface w-full max-w-md rounded-[24px] border p-6 text-left shadow-[0_22px_70px_rgba(0,0,0,.25)]">
            <h2 id="bulk-delete-title" className="text-lg font-semibold">Alle Projekte wirklich löschen?</h2>
            <p className="mt-2 text-sm leading-6 text-[var(--muted-foreground)]">Alle Projekte in ClipForge – lokal gerenderte Videodateien, Audio-Dateien, Medienableitungen und Projektdaten – sowie fehlgeschlagene Anfragen werden dauerhaft gelöscht. Wiederverwendbare Caches bleiben erhalten.</p>
            <p className="mt-4 rounded-xl bg-black/[.04] px-3 py-2 text-sm font-semibold">{bulkDeleteSummary(bulkDeletePlan)}</p>
            {(bulkDeletePlan.projects_keeping_learning_record ?? 0) > 0 && <p className="mt-2 text-xs text-[var(--muted-foreground)]">{bulkDeletePlan.projects_keeping_learning_record} auf YouTube hochgeladene{bulkDeletePlan.projects_keeping_learning_record === 1 ? "s Video bleibt" : " Videos bleiben"} in <PageLink href="/videos" className="underline">Videos</PageLink> erhalten (kompakte Analyse- und Lerndaten). YouTube-Videos werden nie gelöscht.</p>}
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

/**
 * Three live German topic suggestions; they change only on a click or "Neue Vorschläge".
 * Only real questions are rendered - never anonymous blank pills - and every other
 * state (loading, fewer strong candidates, none, discovery failure) is stated in words.
 */
function TopicSuggestionChips({ state, onUse, onRefreshAll }: { state: SuggestionState; onUse: (index: number) => void; onRefreshAll: () => void }) {
  const count = state.visible.filter(Boolean).length;
  const loading = state.status === "loading" || state.status === "idle";
  const note = count === 0
    ? (loading ? "Themenvorschläge werden gesucht…" : state.message)
    : count < state.visible.length
      ? (loading ? "Weitere Vorschläge werden gesucht…" : `Gerade ${count === 1 ? "nur ein starker Vorschlag" : `nur ${count} starke Vorschläge`}.`)
      : state.message;
  return (
    <div className="mt-9 flex w-full max-w-[780px] flex-col items-center gap-2">
      {count > 0 && (
        <div className="flex flex-wrap justify-center gap-2" aria-label="Themenvorschläge">
          {state.visible.map((item, index) => item && (
            <button key={item.candidate_id} type="button" title={item.reason || item.rationale || undefined} onClick={() => onUse(index)} className="topic-suggestion cf-surface flex max-w-[250px] flex-col items-start gap-0.5 rounded-2xl border px-4 py-2 text-left text-xs font-medium text-[var(--muted-foreground)] transition-[transform,background-color,border-color,color] duration-150 ease-[cubic-bezier(.23,1,.32,1)] active:scale-[.97] hover:bg-[var(--surface-hover)] hover:text-[var(--foreground)]">
              <span className="text-[var(--foreground)]">{item.question}</span>
              {(item.signal_label || item.reason) && (
                <span className="line-clamp-2 text-[10px] font-normal leading-4">
                  {item.signal_label && <span className="font-semibold uppercase tracking-[.06em] text-[var(--accent)]">{item.signal_label}</span>}
                  {item.signal_label && item.reason ? " · " : ""}
                  {item.reason && <span>Warum es funktionieren könnte: {item.reason}</span>}
                </span>
              )}
            </button>
          ))}
        </div>
      )}
      {note && (
        <p className="topic-suggestion-status flex items-center gap-1.5 text-xs text-[var(--muted-foreground)]" role="status" aria-live="polite">
          {loading && <LoaderCircle className="size-3 animate-spin" aria-hidden />} {note}
        </p>
      )}
      {!(count === 0 && loading) && (
        <button type="button" onClick={onRefreshAll} className="inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-[11px] font-semibold text-[var(--muted-foreground)] hover:bg-[var(--surface-hover)] hover:text-[var(--foreground)]">
          <RefreshCw className="size-3" /> Neue Vorschläge
        </button>
      )}
    </div>
  );
}

/** "2 Projekte · 1 fehlgeschlagene Anfrage · ca. 133 KB" */
function bulkDeleteSummary(plan: BulkProjectDeletePlan): string {
  const parts = [`${plan.project_count} ${plan.project_count === 1 ? "Projekt" : "Projekte"}`];
  const history = plan.history_entry_count ?? 0;
  if (history > 0) parts.push(`${history} fehlgeschlagene ${history === 1 ? "Anfrage" : "Anfragen"}`);
  if (plan.project_count > 0) parts.push(`ca. ${formatBytes(plan.total_bytes)}`);
  return parts.join(" · ");
}

function formatBytes(bytes: number) {
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GB`;
}

function GenerationQueue({ jobs, onRemove, onClear, onCancel, cancelPending, busy }: { jobs: GenerationJob[]; onRemove: (jobId: string) => void; onClear: () => void; onCancel: (jobId: string) => void; cancelPending: ReadonlySet<string>; busy: boolean }) {
  // The job holding the worker (running / cancelling) and ones just cancelled from here.
  const running = jobs.filter((job) => job.status === "running" || job.status === "cancelling" || job.status === "cancelled");
  const waiting = jobs.filter((job) => job.status === "queued");
  const [clearConfirmationOpen, setClearConfirmationOpen] = useState(false);
  return (
    <section id="video-queue-panel" className="queue-card cf-surface mt-5 w-full max-w-[780px] border p-4 text-left" aria-label="Video Queue" aria-live="polite">
      <div className="mb-3 flex items-center justify-between gap-3">
        {jobs.length === 0 ? <p className="text-sm text-[var(--muted-foreground)]">Keine Videos in der Warteschlange.</p> : <span />}
        <Button asChild variant="outline" size="sm" className="shrink-0"><PageLink href="/queue"><ListVideo className="size-3.5" /> Open Queue Overview <ArrowRight className="size-3.5" /></PageLink></Button>
      </div>
      {running.map((item) => {
        const view = cancelView(item, cancelPending);
        const stopped = view === "cancelled";
        return <div key={item.project_id} className={`mb-2 rounded-xl border p-4 ${stopped ? "border-[var(--border)] bg-[var(--surface-hover)]" : "border-[#ff6838]/30 bg-[#ff6838]/5"}`}>
          <div className="flex items-start justify-between gap-3">
            <PageLink href={`/projects/${item.project_id}`} className="min-w-0 flex-1">
              <p className={`flex items-center gap-2 text-xs font-bold ${stopped ? "text-[var(--muted-foreground)]" : "text-[#d94c20]"}`}><span>{view === "cancelling" ? `● ${CANCELLING_LABEL}` : stopped ? CANCELLED_LABEL : "● Wird erstellt"}</span>{!stopped && <span>{Math.round(item.progress * 100)}%</span>}</p>
              <p className="mt-2 break-words text-sm font-semibold">{item.prompt}</p>
            </PageLink>
            {view === "cancel" && <button type="button" onClick={() => onCancel(item.id)} className="queue-cancel cf-text-error shrink-0 text-xs font-semibold hover:underline">{CANCEL_LABEL}</button>}
            {view === "cancelling" && <span className="queue-cancel inline-flex shrink-0 items-center gap-1.5 text-xs font-semibold text-[var(--muted-foreground)]" role="status"><LoaderCircle className="size-3 animate-spin" aria-hidden /> {CANCELLING_LABEL}</span>}
          </div>
          {!stopped && <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-[#ff6838]/15" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(item.progress * 100)}><div className="h-full bg-[#ff6838]" style={{ width: `${Math.round(item.progress * 100)}%` }} /></div>}
          {view === "cancel" && generationTimeLabel(item) && <p className="generation-timing mt-2"><span className="mono">{generationTimeLabel(item)}</span>{item.stage_label && <span className="cf-text-meta"> · {item.stage_label}</span>}</p>}
          {stopped && <p className="mt-1 text-xs text-[var(--muted-foreground)]">Das Projekt bleibt erhalten.</p>}
        </div>;
      })}
      {waiting.length > 0 && <div className="mb-2 mt-4 flex items-center justify-between gap-3"><p className="text-[11px] font-bold uppercase tracking-[.12em] text-[#85857c]">Warteschlange</p><button type="button" disabled={busy} onClick={() => setClearConfirmationOpen(true)} className="text-xs font-semibold text-red-700 hover:text-red-800 disabled:opacity-50">Clear Queue</button></div>}
      {clearConfirmationOpen && <div className="cf-tone-error mb-3 flex items-center justify-between gap-3 rounded-xl border px-3 py-2 text-xs"><span>Clear {waiting.length} queued project{waiting.length === 1 ? "" : "s"}?</span><span className="flex gap-2"><button type="button" disabled={busy} onClick={() => setClearConfirmationOpen(false)}>Cancel</button><button type="button" disabled={busy} onClick={() => { setClearConfirmationOpen(false); onClear(); }} className="font-bold underline">Clear Queue</button></span></div>}
      <div className="grid gap-2">{waiting.map((item) => <div key={item.project_id} className="flex items-start gap-3 rounded-xl border p-3 hover:bg-[var(--surface-hover)]"><PageLink href={`/projects/${item.project_id}`} className="flex min-w-0 flex-1 items-start gap-3"><span className="mono shrink-0 text-xs font-bold text-[#d94c20]">#{item.queue_position}</span><span className="min-w-0"><span className="block break-words text-sm font-semibold">{item.prompt}</span><span className="mt-1 block text-xs text-[var(--muted-foreground)]">In Warteschlange</span></span></PageLink><button type="button" aria-label={`Remove ${item.prompt} from queue`} disabled={busy} onClick={() => onRemove(item.id)} className="shrink-0 text-xs font-semibold text-red-700 hover:text-red-800 disabled:opacity-50">Remove</button></div>)}</div>
    </section>
  );
}
