"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useId, useRef, useState } from "react";
import {
  ArrowLeft,
  AlertTriangle,
  Check,
  ChevronUp,
  CircleDot,
  Download,
  ExternalLink,
  Film,
  History,
  ImagePlus,
  Layers3,
  LoaderCircle,
  MessageSquareText,
  MoreHorizontal,
  RefreshCw,
  RotateCcw,
  RotateCw,
  Send,
  Settings,
  Sparkles,
  Trash2,
  WandSparkles,
} from "lucide-react";
import {
  getProjectChat,
  getReadiness,
  applySceneMediaCandidate,
  deleteProject,
  getSceneMediaCandidates,
  generateSceneImage,
  exportProject,
  mediaUrl,
  renderProject,
  redoProject,
  sendProjectMessage,
  undoProject,
  updateProjectAudio,
  generateProjectSocialMetadata,
  updateProjectSocialMetadata,
  generateProjectThumbnails,
  selectProjectThumbnail,
  API_ORIGIN,
  listProjectMusicTracks,
  updateProjectMusicSelection,
  musicVolumeGain,
  musicDisplayName,
} from "@/lib/api";
import type { ChatMessage, FinalQualityReview, MusicTrack, Project, Readiness, Scene, Source, SceneMediaCandidates } from "@/lib/types";
import { appliedQualityRepairs, qualityReviewSummary, sceneQualityState, unresolvedQualityScenes } from "@/lib/quality-review";
import { tripleHookSummary, type TripleHookSummary } from "@/lib/triple-hook";
import { Brand } from "./brand";
import { Button } from "./ui/button";
import { ThemeToggle } from "./theme-toggle";
import { cn } from "@/lib/utils";

type Tab = "overview" | "script" | "scenes" | "sources";

function AudioControls({ project, disabled, onDirty, onMusicVolumeChange, onSave }: {
  project: Project;
  disabled: boolean;
  onDirty: () => void;
  onMusicVolumeChange: (volume: number) => void;
  onSave: (audio: { voice_volume: number; music_volume: number; music_enabled: boolean }) => Promise<void>;
}) {
  const state = project.revision.state;
  const [voice, setVoice] = useState(state.voice.volume ?? 1);
  const [music, setMusic] = useState(state.music.volume ?? 0.14);
  const [enabled, setEnabled] = useState(state.music.enabled ?? false);
  const [dirty, setDirty] = useState(false);
  const changed = () => { setDirty(true); onDirty(); };
  return <fieldset disabled={disabled} className="workspace-card mt-4 space-y-3 p-4 text-sm">
    <legend className="px-1 font-semibold">Audio</legend>
    <label className="block">Voice · {Math.round(voice * 100)}%
      <input aria-label="Voice volume" type="range" min="0" max="1" step="0.01" value={voice} className="block w-full" onChange={(e) => { setVoice(Number(e.target.value)); changed(); }} />
    </label>
    <p>Music · {state.music.track?.title ?? "No selected track"}</p>
    <label className="block">Music volume · {Math.round(music * 100)}%
      <input aria-label="Music volume" type="range" min="0" max="1" step="0.01" value={music} className="block w-full" onChange={(e) => { const value = Number(e.target.value); setMusic(value); onMusicVolumeChange(value); changed(); }} />
    </label>
    <label className="flex items-center gap-2"><input type="checkbox" checked={!enabled} disabled={!state.music.track} onChange={(e) => { setEnabled(!e.target.checked); changed(); }} />No music</label>
    <Button size="sm" disabled={!dirty || disabled} onClick={() => void onSave({ voice_volume: voice, music_volume: music, music_enabled: enabled })}>{disabled ? "Saving audio…" : "Save audio"}</Button>
    {dirty && <p className="text-xs">Save audio to update the preview and enable export.</p>}
  </fieldset>;
}

function MusicControls({ project, disabled, onChange, onPreview }: { project: Project; disabled: boolean; onChange: (project: Project) => void; onPreview: () => void }) {
  type Mode = "ai_matched" | "all_music";
  const music = project.revision.state.music;
  const [mode, setMode] = useState<Mode>(music.selection?.mode === "automatic" || music.selection?.mode === "ai_matched" ? "ai_matched" : "all_music");
  const [open, setOpen] = useState(false);
  const [tracks, setTracks] = useState<MusicTrack[]>([]);
  const [musicRevision, setMusicRevision] = useState(project.current_revision);
  const [loading, setLoading] = useState(false);
  const [feedback, setFeedback] = useState<string | null>(null);
  const previewUrl = music.track?.id ? `${API_ORIGIN}/api/music/tracks/${encodeURIComponent(music.track.id)}/preview` : null;
  const currentTrackName = musicDisplayName(music.track?.title ?? "No track selected");

  async function load(nextMode: Mode) {
    setMode(nextMode); setLoading(true); setFeedback(null);
    try { const result = await listProjectMusicTracks(project.id, nextMode); setTracks(result.tracks); setMusicRevision(result.revision); }
    catch { setFeedback("Musikkatalog ist derzeit nicht verfügbar."); } finally { setLoading(false); }
  }
  async function select(trackId: string | null) {
    setLoading(true); setFeedback(null);
    try { onChange(await updateProjectMusicSelection(project.id, musicRevision, trackId, mode)); setOpen(false); }
    catch { setFeedback("Musik konnte nicht ausgewählt werden."); } finally { setLoading(false); }
  }
  return <section className="workspace-card mt-4 p-4 text-sm" aria-label="Background music" aria-busy={loading}>
    <div className="flex flex-wrap items-center justify-between gap-2"><div className="min-w-0"><h2 className="font-semibold">Background music</h2><p className="mt-1 truncate text-xs text-[var(--muted-foreground)]" title={currentTrackName} tabIndex={0} aria-label={`Current music track: ${currentTrackName}`}>{currentTrackName}</p></div><Button type="button" size="sm" variant="outline" disabled={disabled || loading} aria-expanded={open} aria-controls="music-track-list" onClick={() => { setOpen((value) => !value); if (!open) void load(mode); }}>Change music</Button></div>
    {previewUrl && <><Button type="button" size="sm" variant="accent" className="mt-3" disabled={disabled} onClick={onPreview}>Preview with Music</Button><audio className="mt-3 w-full" controls preload="none" src={previewUrl} aria-label={`Track preview: ${currentTrackName}`}>Track preview unavailable.</audio></>}
    {!music.track && <p className="mt-3 text-xs text-[var(--muted-foreground)]">You can still export without music.</p>}
    {open && <div id="music-track-list" className="mt-4 min-w-0 overflow-hidden border-t pt-4"><div className="flex flex-wrap gap-2"><Button type="button" size="sm" variant={mode === "ai_matched" ? "accent" : "outline"} aria-pressed={mode === "ai_matched"} disabled={loading} onClick={() => void load("ai_matched")}>AI Matched</Button><Button type="button" size="sm" variant={mode === "all_music" ? "accent" : "outline"} aria-pressed={mode === "all_music"} disabled={loading} onClick={() => void load("all_music")}>All Music</Button></div>{mode === "ai_matched" && <p className="mt-2 text-xs text-[var(--muted-foreground)]">AI-assisted recommendations from the available licensed catalog for this project.</p>}<div className="mt-3 grid min-w-0 gap-2">{tracks.map((track, index) => { const displayName = musicDisplayName(track.title); return <div key={track.id} className="grid min-w-0 grid-cols-[minmax(0,1fr)_auto] items-center gap-2 rounded-[.7rem] border p-2"><div className="min-w-0 overflow-hidden"><p className="truncate font-medium" title={displayName} tabIndex={0} aria-label={`Music track: ${displayName}`}>{displayName}{mode === "ai_matched" && index === 0 ? " · Recommended" : ""}</p><p className="truncate text-xs text-[var(--muted-foreground)]">{track.mood} · {track.energy}</p></div><div className="flex min-w-0 shrink-0 items-center gap-2"><audio controls preload="none" className="h-8 w-[min(8rem,28vw)]" src={`${API_ORIGIN}${track.preview_url}`} aria-label={`Preview track: ${displayName}`} /><Button type="button" size="sm" variant="outline" disabled={loading} onClick={() => void select(track.id)}>Select</Button></div></div>; })}</div>{tracks.length === 0 && !loading && <p className="mt-3 text-xs text-[var(--muted-foreground)]">No usable licensed tracks are available.</p>}<button type="button" className="mt-3 text-xs text-[#d94c20]" disabled={loading} onClick={() => void select(null)}>Use no music</button></div>}
    {feedback && <p role="status" className="mt-2 text-xs text-[var(--muted-foreground)]">{feedback}</p>}
  </section>;
}

const setupLinks: Record<string, { label: string; url: string }> = {
  director: { label: "Get OpenAI key", url: "https://platform.openai.com/api-keys" },
  research: { label: "Get Brave key", url: "https://api-dashboard.search.brave.com/app/keys" },
  media: { label: "Get Pexels key", url: "https://www.pexels.com/api/new/" },
  voice: { label: "Voice setup", url: "https://platform.openai.com/api-keys" },
  alignment: { label: "Alignment setup", url: "https://pypi.org/project/faster-whisper/" },
  render: { label: "Install FFmpeg", url: "https://ffmpeg.org/download.html" },
  storage: { label: "Set up R2", url: "https://developers.cloudflare.com/r2/get-started/" },
  quality_review: { label: "Local checks", url: "https://ffmpeg.org/ffmpeg.html" },
};

export function ProjectWorkspace({
  project,
  onProjectChange,
}: {
  project: Project;
  onProjectChange: (project: Project) => void;
}) {
  const router = useRouter();
  const [tab, setTab] = useState<Tab>("overview");
  const [message, setMessage] = useState("");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [loadingChat, setLoadingChat] = useState(true);
  const [sending, setSending] = useState(false);
  const [lastFailedMessage, setLastFailedMessage] = useState<string | null>(null);
  const [chatError, setChatError] = useState<string | null>(null);
  const [mobileChatOpen, setMobileChatOpen] = useState(false);
  const [busy, setBusy] = useState<"render" | "export" | "undo" | "redo" | "audio" | "delete" | null>(null);
  const [musicPreview, setMusicPreview] = useState(false);
  const [audioDirty, setAudioDirty] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [moreOpen, setMoreOpen] = useState(false);
  const [mediaBusy, setMediaBusy] = useState<number | null>(null);
  const [mediaError, setMediaError] = useState<string | null>(null);
  const [candidateScene, setCandidateScene] = useState<number | null>(null);
  const [candidateSet, setCandidateSet] = useState<SceneMediaCandidates | null>(null);
  const [candidateSelection, setCandidateSelection] = useState<string | null>(null);
  const [generationState, setGenerationState] = useState<GenerationState>({ status: "idle" });
  const [readiness, setReadiness] = useState<Readiness | null>(null);
  const [deleteConfirmationOpen, setDeleteConfirmationOpen] = useState(false);
  const [socialBusy, setSocialBusy] = useState(false);
  const state = project.revision.state;
  const duration = state.duration.actual_seconds ?? state.duration.estimated_seconds;

  useEffect(() => {
    let active = true;
    getReadiness()
      .then((next) => { if (active) setReadiness(next); })
      .catch(() => undefined);
    return () => { active = false; };
  }, [project.current_revision]);

  useEffect(() => {
    let active = true;
    getProjectChat(project.id)
      .then((history) => { if (active) setMessages(history); })
      .catch((reason) => { if (active) setChatError(reason instanceof Error ? reason.message : "Project chat could not be loaded."); })
      .finally(() => { if (active) setLoadingChat(false); });
    return () => { active = false; };
  }, [project.id]);

  async function submitMessage(retryMessage?: string) {
    const content = (retryMessage ?? message).trim();
    if (!content || sending) return;
    const optimistic: ChatMessage = {
      id: `pending-${Date.now()}`,
      role: "user",
      content,
      tool_metadata: {},
      created_at: new Date().toISOString(),
    };
    if (!retryMessage) setMessages((current) => [...current, optimistic]);
    setSending(true);
    setChatError(null);
    setLastFailedMessage(null);
    try {
      const turn = await sendProjectMessage(project.id, content);
      setMessages(turn.messages);
      onProjectChange(turn.project);
      setMessage("");
      if (turn.project.revision.state.render.status === "regeneration_failed") {
        setError(turn.project.revision.state.render.error ?? "The edit was saved, but the updated video could not be rendered. The previous preview is still available.");
      }
    } catch (reason) {
      setChatError(reason instanceof Error ? reason.message : "ClipForge could not answer that message.");
      setLastFailedMessage(content);
    } finally {
      setSending(false);
    }
  }

  async function render() {
    if (busy) return;
    setBusy("render");
    setError(null);
    try {
      onProjectChange(await renderProject(project.id, project.current_revision));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The video could not be rendered.");
    } finally {
      setBusy(null);
    }
  }

  async function chooseSceneMedia(sceneNumber: number) {
    if (busy || mediaBusy !== null) return;
    setMediaBusy(sceneNumber);
    setMediaError(null);
    setCandidateScene(sceneNumber);
    setCandidateSet(null);
    setCandidateSelection(null);
    setGenerationState({ status: "idle" });
    try {
      setCandidateSet(await getSceneMediaCandidates(project.id, sceneNumber));
    } catch (reason) {
      setMediaError(reason instanceof Error ? reason.message : "Alternatives could not be loaded.");
    } finally {
      setMediaBusy(null);
    }
  }

  async function generateSelectedSceneImage(prompt: string | null) {
    if (!candidateScene || mediaBusy !== null) return;
    const sceneNumber = candidateScene;
    setMediaBusy(sceneNumber);
    setMediaError(null);
    setGenerationState({ status: "generating", prompt });
    try {
      const result = await generateSceneImage(project.id, sceneNumber, project.current_revision, prompt);
      // Keep the revision current (the new alternative is persisted server-side).
      onProjectChange(result.project);
      const candidate = result.candidate;
      if (result.status === "generated" && candidate) {
        // Show the new image immediately, pre-selected for Apply.
        setCandidateSet((current) => current && { ...current, candidates: [candidate, ...current.candidates.filter((item) => item.token !== candidate.token)] });
        setCandidateSelection(candidate.token);
      }
      setGenerationState({ status: result.status, message: result.message, prompt });
    } catch (reason) {
      setGenerationState({ status: "failed", message: reason instanceof Error ? reason.message : "The AI image could not be generated.", prompt });
    } finally {
      setMediaBusy(null);
    }
  }

  async function applySelectedMedia() {
    if (!candidateScene || !candidateSelection || mediaBusy !== null) return;
    setMediaBusy(candidateScene);
    setMediaError(null);
    try {
      onProjectChange(await applySceneMediaCandidate(project.id, candidateScene, candidateSelection, project.current_revision));
      setCandidateScene(null);
      setCandidateSet(null);
      setCandidateSelection(null);
    } catch (reason) {
      setMediaError(reason instanceof Error ? reason.message : "The selected media could not be applied.");
    } finally {
      setMediaBusy(null);
    }
  }

  const undo = useCallback(async () => {
    if (busy || !project.can_undo) return;
    setBusy("undo");
    setError(null);
    try {
      onProjectChange(await undoProject(project.id, project.current_revision));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Nothing to undo.");
    } finally {
      setBusy(null);
    }
  }, [busy, onProjectChange, project.can_undo, project.current_revision, project.id]);

  const redo = useCallback(async () => {
    if (busy || !project.can_redo) return;
    setBusy("redo");
    setError(null);
    try {
      onProjectChange(await redoProject(project.id, project.current_revision));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Nothing to redo.");
    } finally {
      setBusy(null);
    }
  }, [busy, onProjectChange, project.can_redo, project.current_revision, project.id]);

  useEffect(() => {
    function handleHistoryShortcut(event: KeyboardEvent) {
      const target = event.target;
      if (
        target instanceof HTMLInputElement ||
        target instanceof HTMLTextAreaElement ||
        target instanceof HTMLSelectElement ||
        (target instanceof HTMLElement && target.isContentEditable)
      ) return;
      const modifier = event.metaKey || event.ctrlKey;
      if (!modifier || event.altKey) return;
      const wantsUndo = event.key.toLowerCase() === "z" && !event.shiftKey;
      const wantsRedo =
        (event.key.toLowerCase() === "z" && event.shiftKey) ||
        (event.ctrlKey && event.key.toLowerCase() === "y");
      if (wantsUndo && project.can_undo && !busy) {
        event.preventDefault();
        void undo();
      } else if (wantsRedo && project.can_redo && !busy) {
        event.preventDefault();
        void redo();
      }
    }
    window.addEventListener("keydown", handleHistoryShortcut);
    return () => window.removeEventListener("keydown", handleHistoryShortcut);
  }, [busy, project.can_redo, project.can_undo, redo, undo]);

  async function exportMp4() {
    if (busy) return;
    setBusy("export");
    setError(null);
    try {
      const result = await exportProject(project.id, project.current_revision);
      onProjectChange(result.project);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The finished MP4 could not be exported.");
    } finally {
      setBusy(null);
    }
  }

  async function confirmProjectDeletion() {
    if (busy) return;
    setBusy("delete");
    setError(null);
    try {
      await deleteProject(project.id);
      setMessages([]);
      router.replace("/");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Das Projekt konnte nicht gelöscht werden.");
      setDeleteConfirmationOpen(false);
      setBusy(null);
    }
  }

  return (
    <main className="theme-app app-shell min-h-screen bg-[var(--background)]">
      <div className="noise" />
      <header className="page-header sticky top-0 z-40 border-b backdrop-blur-xl">
        <div className="relative mx-auto flex min-h-16 max-w-[1600px] items-center gap-2 px-3 py-2 sm:gap-4 sm:px-5 lg:px-7">
          <Link href="/" aria-label="Back to start" className="interactive-icon">
            <ArrowLeft className="size-4" />
          </Link>
          <div className="hidden sm:block"><Brand /></div>
          <div className="mx-1 hidden h-5 w-px bg-black/10 md:block" />
          <div className="min-w-0 flex-1">
            <p className="truncate text-sm font-semibold tracking-[-.02em]">{project.title}</p>
            <p className="mono hidden text-[9px] uppercase tracking-[.12em] text-[#929289] xs:block sm:block">Project {project.id.slice(0, 8)}</p>
          </div>
          <div className="hidden items-center gap-2 rounded-full border border-emerald-700/10 bg-emerald-50 px-3 py-1.5 text-[11px] font-bold text-emerald-700 md:flex">
            <CircleDot className="size-3" /> {state.render.status === "complete" ? "Rendered" : state.render.stale ? "Previous preview" : "Ready"}
          </div>
          <div className="flex items-center gap-1 lg:hidden">
            <button onClick={() => void undo()} disabled={!!busy || !project.can_undo} className="interactive-icon" aria-label="Undo latest edit" title="Undo (⌘/Ctrl+Z)">
              {busy === "undo" ? <LoaderCircle className="size-4 animate-spin" /> : <RotateCcw className="size-4" />}
            </button>
            <button onClick={() => void redo()} disabled={!!busy || !project.can_redo} className="interactive-icon" aria-label="Redo latest undone edit" title="Redo (⌘+Shift+Z / Ctrl+Y)">
              {busy === "redo" ? <LoaderCircle className="size-4 animate-spin" /> : <RotateCw className="size-4" />}
            </button>
          </div>
          <ThemeToggle />
          {state.render.url ? (
            <Button variant="outline" size="sm" onClick={() => void exportMp4()} disabled={!!busy || audioDirty}>
              {busy === "export" ? <LoaderCircle className="size-3.5 animate-spin" /> : state.export?.status === "exported" ? <Check className="size-3.5" /> : <Download className="size-3.5" />}
              <span className="hidden sm:inline">{busy === "export" ? "Exporting…" : state.export?.status === "exported" ? "Exported" : "Export MP4"}</span>
            </Button>
          ) : null}
          {state.render.status !== "complete" && (
            <Button variant="accent" size="sm" onClick={() => void render()} disabled={!!busy || state.render.status === "blocked_by_research"}>
              {busy === "render" ? <LoaderCircle className="size-3.5 animate-spin" /> : <Film className="size-3.5" />}
              <span className="hidden sm:inline">Render video</span>
            </Button>
          )}
          <Button asChild variant="ghost" size="icon">
            <Link href="/settings/integrations" aria-label="Settings"><Settings className="size-4" /></Link>
          </Button>
          <Button variant="ghost" size="icon" aria-label="Project history" aria-expanded={moreOpen} onClick={() => setMoreOpen((open) => !open)}>
            <MoreHorizontal className="size-5" />
          </Button>
          {moreOpen && (
            <div className="cf-surface absolute right-3 top-[calc(100%+.4rem)] z-50 w-[min(330px,calc(100vw-1.5rem))] origin-top-right rounded-[18px] border p-3 shadow-[0_18px_55px_rgba(30,27,17,.18)]">
              <div className="mb-2 flex items-center gap-2 px-2 py-1 text-xs font-bold"><History className="size-3.5 text-[#ff6838]" /> Revision history</div>
              <div className="max-h-64 space-y-1 overflow-y-auto">
                {project.revisions.map((revision) => (
                  <div key={revision.id} className={cn("rounded-xl px-3 py-2 text-xs", revision.is_current ? "bg-[#ff6838]/10" : "bg-black/[.025]")}>
                    <div className="flex items-center justify-between gap-2">
                      <span className="font-semibold">v{revision.number}{revision.kind === "system" ? " · system" : ""}</span>
                      {revision.is_current && <span className="text-[9px] font-bold uppercase text-[#d94c20]">Current</span>}
                    </div>
                    <p className="mt-1 truncate text-[#77776d]">{revision.instruction}</p>
                  </div>
                ))}
              </div>
              <div className="mt-3 border-t pt-3">
                <Button variant="outline" size="sm" className="w-full border-red-200 text-red-700 hover:bg-red-50" onClick={() => { setMoreOpen(false); setDeleteConfirmationOpen(true); }}>
                  <Trash2 className="size-3.5" /> Projekt löschen
                </Button>
              </div>
            </div>
          )}
        </div>
      </header>

      {deleteConfirmationOpen && (
        <div className="fixed inset-0 z-[70] grid place-items-center bg-black/45 p-4" role="dialog" aria-modal="true" aria-labelledby="delete-project-title">
          <div className="cf-surface w-full max-w-md rounded-[24px] border p-6 shadow-[0_22px_70px_rgba(0,0,0,.25)]">
            <h2 id="delete-project-title" className="text-lg font-semibold">Projekt wirklich löschen?</h2>
            <p className="mt-2 text-sm leading-6 text-[var(--muted-foreground)]">Das Projekt und seine zugehörigen Dateien werden dauerhaft gelöscht.</p>
            <div className="mt-6 flex justify-end gap-2">
              <Button variant="ghost" onClick={() => setDeleteConfirmationOpen(false)} disabled={busy === "delete"}>Abbrechen</Button>
              <Button variant="accent" onClick={() => void confirmProjectDeletion()} disabled={busy === "delete"}>
                {busy === "delete" ? <LoaderCircle className="size-3.5 animate-spin" /> : <Trash2 className="size-3.5" />} Projekt löschen
              </Button>
            </div>
          </div>
        </div>
      )}

      <div className="mx-auto grid max-w-[1600px] grid-cols-1 lg:grid-cols-[minmax(0,1fr)_360px]">
        <section className="results-canvas min-w-0 border-black/8 px-4 pb-32 pt-6 lg:border-r lg:px-8 lg:pb-10">
          <div className="mx-auto max-w-[1180px]">
            {error && <div role="alert" className="mb-5 rounded-[16px] border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-800">{error}</div>}
            {state.export?.status === "exported" && (
              <div role="status" className="mb-5 rounded-[16px] border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm text-emerald-900">
                <p className="font-bold">Exported</p>
                <p className="mt-0.5 break-all text-xs">{state.export.display_path}</p>
                <p className="mt-1 text-xs text-emerald-800">
                  {state.export.cleanup_status === "complete"
                    ? "Temporary project media was cleaned."
                    : state.export.cleanup_warnings.join(" ")}
                </p>
              </div>
            )}
            <div className="results-primary grid items-start gap-6 md:grid-cols-[minmax(300px,.82fr)_minmax(0,1.18fr)]">
              <div>
                <VideoPreview key={state.render.url ?? `${project.id}:${project.current_revision}`} project={project} onRender={() => void render()} rendering={busy === "render"} musicPreview={musicPreview} />
              </div>
              <div className="min-w-0">
                <div className="mb-5 flex flex-wrap items-center gap-2">
                  <Badge>{state.intent.content_type.replaceAll("_", " ")}</Badge>
                  <Badge>{state.intent.tone.replaceAll("_", " ")}</Badge>
                  <Badge>{state.intent.language.toUpperCase()}</Badge>
                </div>
                <h1 className="balance text-3xl font-semibold leading-[1.08] tracking-[-.045em] md:text-4xl">{project.original_prompt}</h1>
                <div className="mt-6 grid grid-cols-3 gap-2">
                  <Stat label="Duration" value={formatTime(duration)} hint={state.duration.actual_seconds ? "measured" : "estimated"} />
                  <Stat label="Scenes" value={String(state.scenes.length)} hint={state.timeline.aspect_ratio} />
                  <Stat label="Revision" value={`v${project.current_revision}`} hint={`${project.revisions.length} saved`} />
                </div>
                {state.render.url && <QualityReviewPanel review={state.final_quality_review} renderRevision={state.render.revision} disabled={!!busy || mediaBusy !== null} onFixScene={(sceneNumber) => { setTab("scenes"); void chooseSceneMedia(sceneNumber); requestAnimationFrame(() => document.getElementById(`scene-row-${sceneNumber}`)?.scrollIntoView({ block: "center", behavior: "smooth" })); }} />}
                {state.render.url && <div className="mt-6 space-y-4">
                  <AudioControls key={`${project.id}:${project.current_revision}`} project={project} disabled={!!busy || sending} onDirty={() => setAudioDirty(true)} onMusicVolumeChange={(volume) => onProjectChange({ ...project, revision: { ...project.revision, state: { ...project.revision.state, music: { ...project.revision.state.music, volume } } } })} onSave={async (audio) => {
                    setBusy("audio");
                    setError(null);
                    try {
                      onProjectChange(await updateProjectAudio(project.id, project.current_revision, audio));
                      setAudioDirty(false);
                    } catch (reason) {
                      setError(reason instanceof Error ? reason.message : "Audio settings could not be saved.");
                    } finally { setBusy(null); }
                  }} />
                  <MusicControls project={project} disabled={!!busy || sending} onChange={onProjectChange} onPreview={() => setMusicPreview(true)} />
                </div>}
              </div>
            </div>

            <SocialMetadata key={`social:${project.id}:${project.current_revision}`} project={project} busy={socialBusy} onChange={onProjectChange} setBusy={setSocialBusy} />
            <ThumbnailControls key={`thumbnail:${project.id}:${project.current_revision}`} project={project} disabled={!!busy || sending} onChange={onProjectChange} />

            <div className="mt-8 border-b border-black/10">
              <nav className="flex gap-1 overflow-x-auto" aria-label="Project detail tabs" role="tablist">
                {(["overview", "script", "scenes", "sources"] as Tab[]).map((item) => (
                  <button type="button" role="tab" aria-selected={tab === item} key={item} onClick={() => setTab(item)} className={cn(
                    "relative px-4 py-3 text-sm font-semibold capitalize text-[#818177] transition-colors duration-150 ease-[cubic-bezier(.23,1,.32,1)] hover:text-black",
                    tab === item && "text-black after:absolute after:inset-x-4 after:bottom-[-1px] after:h-0.5 after:rounded-full after:bg-[#ff6838]",
                  )}>{item}</button>
                ))}
              </nav>
            </div>
            <div className="py-6">
              {tab === "overview" && <Overview project={project} readiness={readiness} />}
              {tab === "script" && <ScriptView project={project} />}
              {tab === "scenes" && <ScenesView scenes={state.scenes} qualityReview={state.final_quality_review} duration={duration} assets={state.assets} mediaBusy={mediaBusy} mediaError={mediaError} candidateScene={candidateScene} candidateSet={candidateSet} candidateSelection={candidateSelection} onSelectCandidate={setCandidateSelection} onChooseSceneMedia={chooseSceneMedia} onApplyCandidate={applySelectedMedia} onGenerateImage={generateSelectedSceneImage} generationState={generationState} onCancel={() => { setCandidateScene(null); setCandidateSet(null); setCandidateSelection(null); setGenerationState({ status: "idle" }); }} />}
              {tab === "sources" && <SourcesView project={project} />}
            </div>
          </div>
        </section>

        <aside className="fixed inset-x-0 bottom-0 z-30 border-t border-[var(--border)] bg-[var(--surface-elevated)] p-3 shadow-[0_-10px_35px_rgba(40,35,20,.08)] backdrop-blur-xl lg:sticky lg:top-16 lg:h-[calc(100vh-4rem)] lg:border-t-0 lg:bg-[var(--surface-subtle)] lg:p-0 lg:shadow-none">
          <div className="hidden h-full flex-col lg:flex">
            <div className="border-b border-black/8 px-6 py-5">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2 text-sm font-bold"><MessageSquareText className="size-4 text-[#ff6838]" /> Project assistant</div>
                  <div className="flex items-center gap-1">
                    <button onClick={() => void undo()} disabled={!!busy || !project.can_undo} className="interactive-text" aria-label="Undo latest edit" title="Undo (⌘/Ctrl+Z)">
                      {busy === "undo" ? <LoaderCircle className="size-3.5 animate-spin" /> : <RotateCcw className="size-3.5" />} Undo
                    </button>
                    <button onClick={() => void redo()} disabled={!!busy || !project.can_redo} className="interactive-text" aria-label="Redo latest undone edit" title="Redo (⌘+Shift+Z / Ctrl+Y)">
                      {busy === "redo" ? <LoaderCircle className="size-3.5 animate-spin" /> : <RotateCw className="size-3.5" />} Redo
                    </button>
                  </div>
              </div>
              <p className="mt-2 text-xs leading-5 text-[#88887f]">Ask about this project or describe a change in your own words.</p>
            </div>
            <ChatMessages messages={messages} loading={loadingChat} sending={sending} project={project} />
            <ChatError error={chatError} retry={lastFailedMessage ? () => void submitMessage(lastFailedMessage) : null} />
            <Composer value={message} setValue={setMessage} submit={() => void submitMessage()} busy={sending} />
          </div>
          <div className="lg:hidden">
            {mobileChatOpen && (
              <div className="cf-surface mb-3 flex max-h-[60vh] flex-col overflow-hidden rounded-[22px] border shadow-2xl">
                <div className="flex items-center justify-between border-b border-black/8 px-4 py-3">
                  <div className="flex items-center gap-2 text-sm font-bold"><MessageSquareText className="size-4 text-[#ff6838]" /> Project assistant</div>
                  <button className="interactive-icon !size-8" onClick={() => setMobileChatOpen(false)} aria-label="Close project assistant"><ChevronUp className="size-4 rotate-180" /></button>
                </div>
                <ChatMessages messages={messages} loading={loadingChat} sending={sending} project={project} compact />
                <ChatError error={chatError} retry={lastFailedMessage ? () => void submitMessage(lastFailedMessage) : null} />
              </div>
            )}
            <button className="mb-2 flex w-full items-center justify-between px-2 text-xs font-bold" onClick={() => setMobileChatOpen((open) => !open)} aria-expanded={mobileChatOpen}>
              <span className="flex items-center gap-2"><MessageSquareText className="size-3.5 text-[#ff6838]" /> {mobileChatOpen ? "Hide conversation" : "Open project assistant"}</span>
              <ChevronUp className={cn("size-4 transition-transform", !mobileChatOpen && "rotate-180")} />
            </button>
            <Composer value={message} setValue={setMessage} submit={() => void submitMessage()} busy={sending} compact />
          </div>
        </aside>
      </div>
    </main>
  );
}

function VideoPreview({ project, onRender, rendering, musicPreview }: { project: Project; onRender: () => void; rendering: boolean; musicPreview: boolean }) {
  const state = project.revision.state;
  const hook = state.script.blocks[0]?.text ?? project.original_prompt;
  const source = mediaUrl(state.render.url);
  const vertical = state.timeline.height > state.timeline.width;
  const [videoUnavailable, setVideoUnavailable] = useState(false);
  const [activeScene, setActiveScene] = useState<number | null>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const audioRef = useRef<HTMLAudioElement>(null);
  const navigableScenes = (state.scenes ?? []).filter((scene) => Number.isFinite(scene.start) && Number.isFinite(scene.end) && scene.end > scene.start);
  const musicSource = state.music.enabled && state.music.track?.id
    ? `${API_ORIGIN}/api/music/tracks/${encodeURIComponent(state.music.track.id)}/preview`
    : null;
  const musicVolume = state.music.volume ?? 0.14;
  const musicDucking = state.music.ducking ?? true;
  useEffect(() => {
    const audio = audioRef.current;
    if (audio) audio.volume = musicVolumeGain(musicVolume, musicDucking);
  }, [musicVolume, musicDucking, musicSource, musicPreview]);
  useEffect(() => {
    if (!musicPreview) audioRef.current?.pause();
  }, [musicPreview]);
  useEffect(() => {
    const video = videoRef.current;
    const audio = audioRef.current;
    if (video && audio && musicPreview && !video.paused) {
      audio.currentTime = video.currentTime;
      void audio.play();
    }
  }, [musicSource, musicPreview]);
  function seekToScene(start: number) {
    const video = videoRef.current;
    if (!video) return;
    const wasPlaying = !video.paused;
    video.currentTime = start;
    if (wasPlaying) void video.play();
  }
  function updateActiveScene(currentTime: number) {
    const scene = navigableScenes.find((item) => currentTime >= item.start && currentTime < item.end) ?? navigableScenes.at(-1);
    setActiveScene(scene ? navigableScenes.indexOf(scene) : null);
  }
  const label = source ? (state.render.stale ? "Previous revision preview" : "Rendered preview") : "Storyboard preview";
  return (
    <div className="preview-stage mx-auto w-full p-1.5" style={{ maxWidth: vertical ? 292 : 520 }}>
      <div
        className={cn(
          "relative overflow-hidden border-[6px] border-[#1c1c19] bg-[#12120f] shadow-[0_25px_60px_rgba(30,27,17,.26)]",
          vertical ? "rounded-[30px]" : "rounded-[22px]",
        )}
        style={{ aspectRatio: `${state.timeline.width} / ${state.timeline.height}` }}
      >
        {source && !videoUnavailable ? (
          <video ref={videoRef} key={source} src={source} controls playsInline preload="metadata" className="size-full bg-black object-contain" aria-label="ClipForge video preview" onError={() => setVideoUnavailable(true)}
            onPlay={() => { if (musicPreview && musicSource) void audioRef.current?.play(); }}
            onPause={() => audioRef.current?.pause()}
            onEnded={() => audioRef.current?.pause()}
            onLoadedMetadata={(event) => { const audio = audioRef.current; if (audio) audio.currentTime = event.currentTarget.currentTime; }}
            onTimeUpdate={(event) => { const audio = audioRef.current; if (audio && Math.abs(audio.currentTime - event.currentTarget.currentTime) > 0.35) audio.currentTime = event.currentTarget.currentTime; updateActiveScene(event.currentTarget.currentTime); }} />
        ) : (
          <>
            <div className="absolute inset-0 bg-[radial-gradient(circle_at_66%_22%,#ffb178_0,transparent_22%),radial-gradient(circle_at_30%_70%,#253449_0,transparent_30%),linear-gradient(155deg,#7d2a16_0%,#191914_45%,#080809_100%)]" />
            <div className="absolute inset-0 bg-gradient-to-b from-black/10 via-transparent to-black/75" />
            <div className="absolute left-4 right-4 top-4 flex items-center justify-between text-[8px] font-bold uppercase tracking-[.13em] text-white/75">
              <span>ClipForge / Preview</span><span>{state.timeline.aspect_ratio}</span>
            </div>
            <div className="absolute inset-x-4 bottom-[21%] text-center">
              <p
                className="inline rounded-lg bg-black/60 px-2 py-1 font-extrabold uppercase leading-[1.35] tracking-[-.045em] text-white shadow-[0_3px_16px_rgba(0,0,0,.85)] backdrop-blur-sm"
                style={{ fontSize: Math.max(16, state.captions.font_size * 0.3) }}
              >
                {hook.split(" ").slice(0, 11).join(" ")}
              </p>
              {source && videoUnavailable && <div role="alert" className="mt-3 rounded-xl bg-black/70 px-3 py-2 text-xs text-white">Die gespeicherte Videodatei ist nicht verfügbar. <button onClick={onRender} disabled={rendering} className="ml-1 font-bold text-[#ffb178] underline">{rendering ? "Wird gerendert…" : "Video erneut rendern"}</button></div>}
              <span className="mt-2 inline-block h-1 w-12 rounded-full bg-[#ff6838]" />
            </div>
          </>
        )}
      </div>
      {musicPreview && musicSource && <audio ref={audioRef} src={musicSource} preload="auto" aria-hidden="true" />}
      <p className="mt-3 text-center text-[10px] font-semibold uppercase tracking-[.12em] text-[#88887f]">{label} · {state.timeline.aspect_ratio}</p>
      {navigableScenes.length > 0 && <div className="mt-3 rounded-xl border border-[var(--border)] bg-[var(--surface-elevated)] p-2 text-left" aria-label="Scene navigation">
        <p className="px-2 pb-1 text-[10px] font-bold uppercase tracking-[.12em] text-[var(--muted-foreground)]">Scenes</p>
        <div className="grid max-h-40 gap-0.5 overflow-y-auto">
          {navigableScenes.map((scene, index) => {
            const sceneKey = scene.id || String(index);
            const isActive = activeScene === index;
            return <button key={sceneKey} type="button" className={cn("flex min-w-0 items-center justify-between rounded-lg px-2 py-1.5 text-xs transition-colors", isActive ? "bg-[var(--accent-soft)] font-semibold text-[var(--foreground)]" : "text-[var(--muted-foreground)] hover:bg-[var(--surface-hover)] hover:text-[var(--foreground)]")} aria-current={isActive ? "true" : undefined} onClick={() => seekToScene(scene.start)}><span className="flex min-w-0 items-center gap-2"><span className={cn("size-1.5 shrink-0 rounded-full", isActive ? "bg-[var(--accent)]" : "bg-[var(--border)]")} /><span>Scene {index + 1}</span></span><span className="mono shrink-0 text-[10px]">{formatTime(scene.start)}–{formatTime(scene.end)}</span></button>;
          })}
        </div>
      </div>}
    </div>
  );
}

function Pipeline({ stages }: { stages: Project["revision"]["state"]["pipeline"] }) {
  return (
    <div className="cf-surface mt-6 rounded-[20px] border p-4 shadow-sm">
      <div className="mb-3 flex items-center justify-between">
        <p className="text-xs font-bold uppercase tracking-[.12em] text-[#77776d]">Production map</p>
        <span className="mono text-[9px] text-[#9a9a91]">AUTO</span>
      </div>
      <div className="grid grid-cols-4 gap-x-2 gap-y-3">
        {stages.map((stage, index) => (
          <div key={stage.id} className="relative min-w-0">
            <div className={cn(
              "mb-1.5 grid size-5 place-items-center rounded-full border text-[9px]",
              stage.status === "complete" ? "border-[#ff6838] bg-[#ff6838] text-white" : stage.status === "blocked" ? "border-red-200 bg-red-50 text-red-700" : "border-black/10 bg-white text-[#aaa]",
            )}>{stage.status === "complete" ? <Check className="size-3" /> : index + 1}</div>
            <p className="truncate text-[10px] font-semibold text-[#77776d]">{stage.label.replace(" your idea", "")}</p>
          </div>
        ))}
      </div>
    </div>
  );
}

function SocialMetadata({ project, busy, onChange, setBusy }: {
  project: Project;
  busy: boolean;
  onChange: (project: Project) => void;
  setBusy: (busy: boolean) => void;
}) {
  const metadata = project.revision.state.social_metadata;
  type Platform = "tiktok" | "instagram" | "youtube";
  type Draft = { title: string; description: string; hashtags: string };
  const [drafts, setDrafts] = useState<Record<Platform, Draft>>(() => {
    const platforms = metadata?.platforms ?? {};
    return {
      tiktok: { title: platforms.tiktok?.title ?? "", description: platforms.tiktok?.description ?? "", hashtags: (platforms.tiktok?.hashtags ?? []).join(" ") },
      instagram: { title: platforms.instagram?.title ?? "", description: platforms.instagram?.description ?? "", hashtags: (platforms.instagram?.hashtags ?? []).join(" ") },
      youtube: { title: platforms.youtube?.title ?? "", description: platforms.youtube?.description ?? "", hashtags: (platforms.youtube?.hashtags ?? []).join(" ") },
    };
  });
  const [feedback, setFeedback] = useState<string | null>(null);
  const parse = (value: string) => value.split(/[,\s]+/).filter(Boolean);
  async function save() {
    setBusy(true); setFeedback(null);
    try {
      onChange(await updateProjectSocialMetadata(project.id, project.current_revision, {
        tiktok: parse(drafts.tiktok.hashtags), instagram: parse(drafts.instagram.hashtags), youtube: parse(drafts.youtube.hashtags),
      }, {
        tiktok: { title: drafts.tiktok.title, description: drafts.tiktok.description },
        instagram: { title: drafts.instagram.title, description: drafts.instagram.description },
        youtube: { title: drafts.youtube.title, description: drafts.youtube.description },
      }));
      setFeedback("Gespeichert");
    } catch { setFeedback("Hashtags konnten nicht gespeichert werden."); } finally { setBusy(false); }
  }
  async function regenerate(platform?: "tiktok" | "instagram" | "youtube") {
    setBusy(true); setFeedback(null);
    try { onChange(await generateProjectSocialMetadata(project.id, project.current_revision, platform)); }
    catch { setFeedback("Hashtags konnten nicht generiert werden."); } finally { setBusy(false); }
  }
  async function copy(value: string) {
    try { await navigator.clipboard.writeText(value); setFeedback("Kopiert"); }
    catch { setFeedback("Kopieren nicht verfügbar."); }
  }
  return <section className="workspace-card mt-8 p-5" aria-label="Social Metadata">
    <div className="flex flex-wrap items-center justify-between gap-3"><div><h2 className="font-semibold">Social Metadata</h2><p className="mt-1 text-xs text-[var(--muted-foreground)]">Titel, Beschreibung und Hashtags für jede Plattform.</p></div><Button size="sm" variant="outline" disabled={busy} onClick={() => void regenerate()}>{busy ? <LoaderCircle className="size-3.5 animate-spin" /> : <RefreshCw className="size-3.5" />}{metadata ? "Neu generieren" : "Metadaten generieren"}</Button></div>
    {metadata?.status === "unavailable" && <p className="mt-3 text-sm text-[var(--muted-foreground)]">Hashtags derzeit nicht verfügbar. Du kannst es erneut versuchen.</p>}
    <div className="mt-4 grid gap-3 md:grid-cols-3">{(["tiktok", "instagram", "youtube"] as const).map((platform) => <div key={platform} className="cf-subtle min-w-0 rounded-[.85rem] border p-3"><div className="flex items-center justify-between gap-2"><strong className="text-sm">{{ tiktok: "TikTok", instagram: "Instagram", youtube: "YouTube" }[platform]}</strong><button className="interactive-text text-xs" onClick={() => void copy(`${drafts[platform].title}\n\n${drafts[platform].description}\n\n${drafts[platform].hashtags}`)}>Copy all</button></div><div className="mt-3 space-y-2 rounded-[.7rem] border border-[var(--border)] bg-[var(--input)] p-3"><label className="block text-[11px] font-semibold">Title<input aria-label={`${platform} title`} value={drafts[platform].title} onChange={(event) => setDrafts((current) => ({ ...current, [platform]: { ...current[platform], title: event.target.value } }))} className="cf-input mt-1 text-xs" /></label><label className="block text-[11px] font-semibold">Description<textarea aria-label={`${platform} description`} value={drafts[platform].description} onChange={(event) => setDrafts((current) => ({ ...current, [platform]: { ...current[platform], description: event.target.value } }))} className="cf-input mt-1 min-h-16 resize-y text-xs" /></label><label className="block text-[11px] font-semibold">Hashtags<textarea aria-label={`${platform} hashtags`} value={drafts[platform].hashtags} onChange={(event) => setDrafts((current) => ({ ...current, [platform]: { ...current[platform], hashtags: event.target.value } }))} className="cf-input mt-1 min-h-16 resize-y text-xs" placeholder="#Hashtags" /></label></div><button className="mt-2 text-xs text-[#d94c20]" disabled={busy} onClick={() => void regenerate(platform)}>Neu generieren</button></div>)}</div>
    <div className="mt-4 flex items-center gap-3"><Button size="sm" disabled={busy} onClick={() => void save()}>Metadaten speichern</Button>{feedback && <span role="status" className="text-xs text-[var(--muted-foreground)]">{feedback}</span>}</div>
  </section>;
}

function ThumbnailControls({ project, disabled, onChange }: { project: Project; disabled: boolean; onChange: (project: Project) => void }) {
  const thumbnails = project.revision.state.thumbnails;
  const [busy, setBusy] = useState(false);
  const [feedback, setFeedback] = useState<string | null>(null);
  async function generate() {
    setBusy(true); setFeedback(null);
    try { onChange(await generateProjectThumbnails(project.id, project.current_revision)); }
    catch (reason) { setFeedback(reason instanceof Error ? reason.message : "Cover konnte nicht erstellt werden."); }
    finally { setBusy(false); }
  }
  async function select(variantId: string) {
    setBusy(true); setFeedback(null);
    try { onChange(await selectProjectThumbnail(project.id, project.current_revision, variantId)); }
    catch (reason) { setFeedback(reason instanceof Error ? reason.message : "Cover konnte nicht ausgewählt werden."); }
    finally { setBusy(false); }
  }
  return <section className="workspace-card mt-8 p-5" aria-label="Cover thumbnails">
    <div className="flex flex-wrap items-center justify-between gap-3"><div><h2 className="font-semibold">Cover</h2><p className="mt-1 text-xs text-[var(--muted-foreground)]">Projektbezogene Varianten für Shorts, TikTok und Reels.</p></div><Button size="sm" variant="outline" disabled={disabled || busy} onClick={() => void generate()}>{busy ? <LoaderCircle className="size-3.5 animate-spin" /> : <RefreshCw className="size-3.5" />}{thumbnails?.status === "available" ? "Neu erstellen" : "Cover erstellen"}</Button></div>
    {thumbnails?.status === "unavailable" && <p className="mt-3 text-sm text-[var(--muted-foreground)]">{thumbnails.error ?? "Keine geeigneten Projektbilder verfügbar."}</p>}
    {thumbnails?.variants?.length ? <div className="mt-4 grid grid-cols-3 gap-3">{thumbnails.variants.map((variant) => <button key={variant.id} type="button" disabled={disabled || busy} onClick={() => void select(variant.id)} className={cn("overflow-hidden rounded-xl border-2 text-left transition", thumbnails.selected_variant_id === variant.id ? "border-[#ff6838] ring-2 ring-[#ff6838]/20" : "border-transparent hover:border-black/20")} aria-label={`Select ${variant.platform} cover`} aria-pressed={thumbnails.selected_variant_id === variant.id}>
      {/* Runtime API media is intentionally not routed through next/image's remote loader. */}
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img src={mediaUrl(variant.url) ?? ""} alt={variant.text ?? "Project cover"} className="aspect-[9/16] w-full object-cover" /><span className="block px-2 py-2 text-[10px] font-bold uppercase tracking-[.08em]">{variant.platform}{thumbnails.selected_variant_id === variant.id ? " · selected" : ""}</span></button>)}</div> : null}
    {feedback && <p role="status" className="mt-3 text-xs text-[var(--muted-foreground)]">{feedback}</p>}
  </section>;
}

function Overview({ project, readiness }: { project: Project; readiness: Readiness | null }) {
  const state = project.revision.state;
  const projectReadiness = readiness ? { ...readiness } : null;
  if (projectReadiness && state.captions.timing === "word_aligned") {
    projectReadiness.alignment = { ...projectReadiness.alignment, ready: true, status: "Word alignment ready" };
  } else if (projectReadiness && state.captions.timing === "phrase_fallback") {
    const missing = state.captions.diagnostic?.toLowerCase().includes("dependency missing");
    projectReadiness.alignment = {
      ...projectReadiness.alignment,
      ready: false,
      status: missing ? "Alignment dependency missing" : "Alignment failed — phrase timing fallback used",
    };
  }
  const rows = projectReadiness
    ? Object.entries(projectReadiness)
    : Object.entries(state.integrations).map(([name, status]) => [name, { ready: !status.includes("unavailable"), status, key: null, url: setupLinks[name]?.url ?? "#" }] as const);
  return (
    <div className="grid gap-4 md:grid-cols-2">
      <Pipeline stages={state.pipeline} />
      <AIReviewPanel review={state.ai_review} />
      <Panel icon={<WandSparkles className="size-4" />} title="Creative direction">
        <div className="space-y-3 text-sm">
          <KeyValue label="Story type" value={state.intent.content_type.replaceAll("_", " ")} />
          <KeyValue label="Tone" value={state.intent.tone.replaceAll("_", " ")} />
          <KeyValue label="Voice" value={state.voice.profile.replaceAll("_", " ")} />
          <KeyValue label="Music" value={state.music.mood.replaceAll("_", " ")} />
          <KeyValue label="Cut pace" value={state.timeline.cut_pace} />
          <OpeningHook hook={tripleHookSummary(state.script.triple_hook)} />
        </div>
      </Panel>
      <Panel icon={<Layers3 className="size-4" />} title="Build readiness">
        <div className="space-y-3">
          {rows.map(([name, item]) => {
            const link = setupLinks[name] ?? { label: "Setup", url: item.url };
            const explanation = readinessExplanation(name, item);
            return (
              <div key={name} className="cf-subtle rounded-xl border p-3 text-xs">
                <div className="flex items-center justify-between gap-3">
                  <span className="font-semibold capitalize text-[var(--foreground)]">{name.replaceAll("_", " ")}</span>
                  <span className={cn("rounded-full px-2 py-1 font-bold", explanation.level === "ready" ? "bg-emerald-50 text-emerald-700" : explanation.level === "degraded" ? "bg-amber-50 text-amber-800" : "bg-red-50 text-red-700")}>{explanation.badge}</span>
                </div>
                <p className="mt-2 leading-5 text-[var(--muted-foreground)]">{explanation.message}</p>
                <div className="mt-2 flex items-center justify-between gap-2 text-[10px] text-[#8b8b82]">
                  <span className="mono truncate">{name === "alignment" ? "Local caption timing" : item.key ?? "No key required"}</span>
                  <a href={link.url} target="_blank" rel="noreferrer" className="inline-flex shrink-0 items-center gap-1 font-bold text-[#d94c20] hover:text-[#a93210]">
                    {link.label} <ExternalLink className="size-2.5" />
                  </a>
                </div>
              </div>
            );
          })}
          <p className="px-1 text-[10px] leading-4 text-[#898980]">
            Manage provider keys in <Link href="/settings/integrations" className="font-bold text-[#d94c20] hover:text-[#a93210]">Settings → Integrations</Link>.
          </p>
        </div>
      </Panel>
      <Panel icon={<Sparkles className="size-4" />} title="Answer skeleton" wide>
        <div className="flex flex-wrap items-center gap-2">
          {state.script.blocks.map((block, index) => (
            <div key={block.id} className="contents">
              <span className="rounded-full border border-black/8 bg-[#faf9f4] px-3 py-2 text-xs font-bold uppercase tracking-[.08em] text-[#595950]">{block.role}</span>
              {index < state.script.blocks.length - 1 && <span className="text-[#b1b1a7]">→</span>}
            </div>
          ))}
        </div>
      </Panel>
    </div>
  );
}

function QualityReviewPanel({ review, renderRevision, disabled, onFixScene }: { review?: FinalQualityReview; renderRevision?: number; disabled: boolean; onFixScene: (sceneNumber: number) => void }) {
  const detailsId = useId();
  const [expanded, setExpanded] = useState(false);
  const summary = qualityReviewSummary(review, renderRevision);
  if (!summary) return null;
  const unresolved = unresolvedQualityScenes(review);
  const repairs = appliedQualityRepairs(review);
  const expandable = unresolved.length > 0 || repairs.length > 0;
  const tone = { passed: "bg-emerald-50 text-emerald-800", repaired: "bg-blue-50 text-blue-800", attention: "bg-amber-50 text-amber-800", muted: "bg-black/[.04] text-[var(--muted-foreground)]" }[summary.tone];
  return (
    <div className="cf-surface mt-3 rounded-[16px] border p-3 text-xs" aria-label="Final quality review">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="font-semibold">Quality review</span>
        {expandable ? (
          <button type="button" onClick={() => setExpanded((open) => !open)} aria-expanded={expanded} aria-controls={detailsId} className={cn("inline-flex items-center gap-1 rounded-full px-2.5 py-1 text-[10px] font-bold", tone)}>
            {summary.label}<ChevronUp className={cn("size-3 transition-transform", !expanded && "rotate-180")} />
          </button>
        ) : <span className={cn("rounded-full px-2.5 py-1 text-[10px] font-bold", tone)}>{summary.label}</span>}
      </div>
      {expandable && expanded && (
        <div id={detailsId} className="mt-2 space-y-3">
          {repairs.length > 0 && (
            <section aria-label="Automatically repaired">
              <p className="mb-1 text-[9px] font-bold uppercase tracking-[.08em] text-emerald-700">Automatically repaired</p>
              <ul className="space-y-1">{repairs.map((repair, index) => <li key={`${repair.sceneId}-${index}`} className="text-[var(--muted-foreground)]"><strong className="text-[var(--foreground)]">Scene {repair.sceneNumber ?? "?"}</strong>: {repair.text}</li>)}</ul>
            </section>
          )}
          {unresolved.length > 0 && (
            <section aria-label="Unresolved">
              <p className="mb-1 text-[9px] font-bold uppercase tracking-[.08em] text-amber-700">Unresolved</p>
              <ul className="space-y-1">{unresolved.map((item) => <li key={item.sceneId} className="flex items-start justify-between gap-2"><span className="text-amber-900"><strong>Scene {item.sceneNumber}</strong>: {item.attempt ?? item.message}{item.issueCount > 1 ? ` (+${item.issueCount - 1} more)` : ""}</span><button type="button" disabled={disabled} onClick={() => onFixScene(item.sceneNumber)} className="shrink-0 text-[10px] font-bold text-[#d94c20] hover:text-[#a93210] disabled:opacity-50" aria-label={`Change media for scene ${item.sceneNumber}`}>Fix</button></li>)}</ul>
            </section>
          )}
        </div>
      )}
    </div>
  );
}

function AIReviewPanel({ review }: { review: Project["revision"]["state"]["ai_review"] }) {
  const detailsId = useId();
  const [expanded, setExpanded] = useState(false);
  const status = review?.status ?? "pending";
  const warning = status === "passed_with_warnings" || status === "needs_fix" || status === "failed" || status === "unavailable";
  const label = status === "passed_with_warnings" ? "Passed with warnings" : status.replaceAll("_", " ");
  const findings = review?.items ?? [];
  const inconsistent = ["needs_fix", "failed", "passed_with_warnings"].includes(status) && findings.length === 0;
  const expandable = warning || findings.length > 0 || Boolean(review?.automatic_corrections?.length);
  return (
    <Panel icon={warning ? <AlertTriangle className="size-4 text-[var(--warning)]" /> : <Check className="size-4 text-[var(--success)]" />} title="AI Review" wide>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-sm text-[var(--muted-foreground)]">Language, prompt fidelity, research, timing, visuals, captions, and render inputs.</p>
        {expandable ? (
          <button type="button" onClick={() => setExpanded((open) => !open)} aria-expanded={expanded} aria-controls={detailsId} className={cn("inline-flex items-center gap-1 rounded-full px-2.5 py-1 text-[10px] font-bold uppercase tracking-[.08em]", warning ? "bg-amber-50 text-amber-800" : "bg-emerald-50 text-emerald-800")}>
            {label}<ChevronUp className={cn("size-3 transition-transform", !expanded && "rotate-180")} />
          </button>
        ) : (
          <span className="rounded-full bg-emerald-50 px-2.5 py-1 text-[10px] font-bold uppercase tracking-[.08em] text-emerald-800">{label}</span>
        )}
      </div>
      {expandable && expanded && (
        <div id={detailsId} className="mt-3 space-y-3">
          {review?.automatic_corrections?.length ? <div className="rounded-xl border border-[var(--border)] bg-[var(--accent-soft)] p-3 text-xs"><strong>Automatically corrected</strong><ul className="mt-1 list-disc space-y-1 pl-4">{review.automatic_corrections.map((item) => <li key={item}>{item}</li>)}</ul></div> : null}
          {findings.length ? <ul className="grid gap-2 sm:grid-cols-2">{findings.map((item, index) => <li key={`${item.check}-${index}`} className="cf-subtle rounded-xl border p-3 text-xs leading-5"><div className="flex items-center justify-between gap-2"><span className="font-bold capitalize">{item.check.replaceAll("_", " ")}</span><span className={cn("rounded-full px-2 py-0.5 text-[9px] font-bold uppercase", item.severity === "error" ? "bg-red-50 text-red-700" : item.severity === "warning" ? "bg-amber-50 text-amber-800" : "bg-blue-50 text-blue-700")}>{item.severity}</span></div><p className="mt-1 text-[var(--muted-foreground)]">{item.message}</p></li>)}</ul> : null}
          {inconsistent && <p role="alert" className="rounded-xl border border-amber-300/50 bg-amber-50 p-3 text-xs font-medium text-amber-900">Review reported “{label}” but supplied no visible findings. Run review again before relying on this status.</p>}
          {status === "unavailable" && <p className="text-xs font-medium text-[var(--warning)]">AI review was unavailable. Local checks remain visible, and factual verification is not claimed.</p>}
        </div>
      )}
      {!expandable && status === "pending" && <p className="mt-3 text-xs text-[var(--muted-foreground)]">Review will run before the next completed render.</p>}
    </Panel>
  );
}

function readinessExplanation(name: string, item: Readiness[string]): { badge: string; message: string; level: "ready" | "degraded" | "blocked" } {
  const status = item.status.toLowerCase();
  if (name === "alignment") {
    if (item.ready || status.includes("word alignment ready")) {
      return { badge: "Word timing ready", message: "Precise word-level caption timing is available.", level: "ready" };
    }
    if (status.includes("preparing")) {
      return { badge: "Preparing", message: "ClipForge is preparing the local caption-alignment model. Video generation can continue with fallback timing meanwhile.", level: "degraded" };
    }
    if (status.includes("dependency missing")) {
      return { badge: "Phrase timing available", message: "Word-level caption timing needs the optional local alignment dependency. Videos can still be generated with less precise phrase timing.", level: "degraded" };
    }
    if (status.includes("model unavailable")) {
      return { badge: "Phrase timing available", message: "The local alignment model is unavailable. Video generation can continue with less precise phrase timing.", level: "degraded" };
    }
    return { badge: "Phrase timing used", message: "The video remains usable, but captions use phrase timing instead of precise active-word alignment.", level: "degraded" };
  }
  if (item.ready) return { badge: item.status.replaceAll("_", " "), message: "This capability is ready for the current project.", level: "ready" };
  return { badge: item.status.replaceAll("_", " "), message: "This capability needs setup before its part of production can run.", level: "blocked" };
}

function ScriptView({ project }: { project: Project }) {
  const state = project.revision.state;
  const blocks = state.script.blocks;
  return (
    <Panel icon={<MessageSquareText className="size-4" />} title={`Narration · ${state.script.word_count} words`}>
      <div className="cf-subtle mb-4 rounded-xl border p-3 text-xs leading-5">
        <div className="flex flex-wrap items-center justify-between gap-2"><strong>Caption timing</strong><span className="mono text-[9px] uppercase text-[var(--muted-foreground)]">{state.captions.timing?.replaceAll("_", " ") ?? "pending"}</span></div>
        {state.captions.diagnostic && <p className="mt-1 text-[var(--muted-foreground)]">{state.captions.diagnostic}</p>}
      </div>
      <div className="space-y-1">
        {blocks.map((block) => (
          <div key={block.id} className="group grid grid-cols-[74px_1fr] gap-3 rounded-xl px-2 py-3 transition-colors duration-150 hover:bg-black/[.025]">
            <span className="mono pt-1 text-[9px] font-medium uppercase tracking-[.1em] text-[#ff6838]">{block.role}</span>
            <p className="text-[15px] leading-7 text-[#383832]">{block.text}</p>
          </div>
        ))}
      </div>
    </Panel>
  );
}

function mediaProvenance(media: NonNullable<Scene["media"]>): string {
  const source = media.source ?? media.provider;
  if (source === "generated_openai") {
    return `AI image · ${media.generation?.model_label ?? media.generation?.model ?? "OpenAI"}`;
  }
  if (source === "simple_graphic") return "Graphic · ClipForge";
  return `${media.kind} by ${media.creator} · ${media.provider}`;
}

type GenerationState = {
  status: "idle" | "generating" | "generated" | "failed" | "rejected" | "unchanged";
  message?: string;
  prompt?: string | null;
};

function candidatePreview(url: string): string {
  // Generated alternatives are served by the API's /media route.
  return url.startsWith("/media/") ? (mediaUrl(url) ?? url) : url;
}

function GenerationStatus({ state, onRetry, busy }: { state: GenerationState; onRetry: () => void; busy: boolean }) {
  if (state.status === "idle") return null;
  if (state.status === "generating") {
    return (
      <p role="status" aria-live="polite" className="inline-flex items-center gap-1.5 text-xs font-semibold">
        <LoaderCircle className="size-3 animate-spin" /> Generating… this can take up to a minute.
      </p>
    );
  }
  if (state.status === "generated") {
    return <p role="status" aria-live="polite" className="media-success text-xs font-semibold">{state.message ?? "AI image generated."}</p>;
  }
  // failed | rejected | unchanged: visible, specific, with a retry.  An
  // unchanged result broke nothing, so it reads as a warning, not an error.
  return (
    <div role="alert" className={cn("flex flex-wrap items-center gap-2 rounded-xl px-3 py-2 text-xs leading-5", state.status === "unchanged" ? "media-warning" : "media-alert")}>
      <span>{state.message ?? "The AI image could not be generated."}</span>
      <button type="button" onClick={onRetry} disabled={busy} className="media-link uppercase tracking-[.08em]">Retry</button>
    </div>
  );
}

function GenerateImageOption({ option, busy, onGenerate, state }: { option: NonNullable<SceneMediaCandidates["generation"]>; busy: boolean; onGenerate: (prompt: string | null) => void; state: GenerationState }) {
  const [editing, setEditing] = useState(false);
  const [prompt, setPrompt] = useState(option.prompt);
  const promptId = useId();
  if (!option.available) {
    return (
      <p className="media-muted text-xs leading-5">
        {option.unavailable_reason === "no_api_key"
          ? "Add an OpenAI API key in Settings to generate an AI image for this scene."
          : "No safe visual subject is available for an AI image at this point of the story."}
      </p>
    );
  }
  return (
    <div className="space-y-2">
      <p className="text-xs leading-5">
        Creates one image from this scene&apos;s visual intent. Model: <strong>{option.model_label}</strong> · Quality: <strong>{option.quality_label}</strong>. This uses paid OpenAI API credits.
      </p>
      {editing && (
        <div>
          <label htmlFor={promptId} className="media-muted mb-1 block text-[10px] font-bold uppercase tracking-[.08em]">Prompt (optional edit)</label>
          <textarea id={promptId} value={prompt} onChange={(event) => setPrompt(event.target.value)} rows={4} maxLength={1200} className="cf-input text-xs leading-5" />
        </div>
      )}
      <div className="flex flex-wrap items-center gap-2">
        {/* An edited prompt is sent verbatim; otherwise the backend's automatic prompt is used. */}
        <button type="button" onClick={() => onGenerate(editing && prompt.trim() !== option.prompt.trim() ? prompt : null)} disabled={busy} className="media-strong inline-flex items-center gap-1.5 rounded-full px-3 py-1.5 text-[10px] font-bold uppercase tracking-[.08em]">
          {state.status === "generating" ? <LoaderCircle className="size-3 animate-spin" /> : <ImagePlus className="size-3" />}
          {state.status === "generating" ? "Generating…" : "Generate AI image"}
        </button>
        <button type="button" onClick={() => setEditing((value) => !value)} disabled={busy} className="media-action rounded-full px-3 py-1.5 text-[10px] font-bold uppercase tracking-[.08em]">
          {editing ? "Use automatic prompt" : "Edit prompt"}
        </button>
      </div>
      <GenerationStatus state={state} busy={busy} onRetry={() => onGenerate(state.prompt ?? null)} />
    </div>
  );
}

function ScenesView({ scenes, qualityReview, duration, assets, mediaBusy, mediaError, candidateScene, candidateSet, candidateSelection, onSelectCandidate, onChooseSceneMedia, onApplyCandidate, onGenerateImage, generationState, onCancel }: { scenes: Scene[]; qualityReview?: FinalQualityReview; duration: number; assets: Project["revision"]["state"]["assets"]; mediaBusy: number | null; mediaError: string | null; candidateScene: number | null; candidateSet: SceneMediaCandidates | null; candidateSelection: string | null; onSelectCandidate: (token: string) => void; onChooseSceneMedia: (sceneNumber: number) => void; onApplyCandidate: () => void; onGenerateImage: (prompt: string | null) => void; generationState: GenerationState; onCancel: () => void }) {
  return (
    <div className="space-y-3">
      {mediaError && (
        <div role="alert" className="rounded-[16px] border border-red-700/15 bg-red-50 px-4 py-3 text-xs leading-5 text-red-700">
          {mediaError}
        </div>
      )}
      {assets.diagnostic && (
        <div className="rounded-[16px] border border-amber-700/15 bg-amber-50 px-4 py-3 text-xs leading-5 text-amber-800">
          {assets.diagnostic}
        </div>
      )}
      <div className="cf-surface relative mb-6 flex h-16 overflow-hidden rounded-[16px] border p-1.5 shadow-sm">
        {scenes.map((scene, index) => (
          <div key={scene.id} title={scene.narration} style={{ width: `${((scene.end - scene.start) / Math.max(1, duration)) * 100}%` }} className={cn("relative min-w-[5%] overflow-hidden border-r border-white/50 last:border-0", ["bg-[#ff8b62]", "bg-[#2f4054]", "bg-[#dbb164]", "bg-[#8b9c77]"][index % 4])}>
            <span className="mono absolute bottom-1.5 left-2 text-[8px] text-white/80">{index + 1}</span>
          </div>
        ))}
      </div>
      {scenes.map((scene, index) => (
        <div key={scene.id} id={`scene-row-${index + 1}`} className="cf-surface grid grid-cols-[46px_minmax(0,1fr)] items-center gap-3 rounded-[18px] border p-3 shadow-sm sm:grid-cols-[54px_minmax(0,1fr)_112px_auto] sm:gap-4">
          <div className={cn("grid aspect-square place-items-center rounded-xl text-sm font-extrabold text-white", ["bg-[#ff7950]", "bg-[#34475d]", "bg-[#c89941]", "bg-[#7c8f67]"][index % 4])}>{index + 1}</div>
          <div className="min-w-0">
            <p className="truncate text-sm font-semibold">{scene.visual_goal}</p>
            <p className="mt-1 truncate text-xs text-[var(--muted-foreground)]">{scene.narration}</p>
            {scene.media && <p className="media-muted mt-1 text-[9px] font-semibold uppercase tracking-[.08em]">{scene.media.source_url ? <a href={scene.media.source_url} target="_blank" rel="noreferrer" className="underline-offset-2 hover:text-[var(--foreground)] hover:underline">{mediaProvenance(scene.media)}</a> : mediaProvenance(scene.media)}</p>}
          </div>
          {scene.media?.cache_path ? (
            <div className="media-box col-span-2 overflow-hidden rounded-xl sm:col-span-1">
              <p className="media-muted px-2 pt-1 text-[9px] font-bold uppercase tracking-[.08em]">Current media</p>
              {scene.media.kind === "video" ? (
                <video
                  src={mediaUrl(`/media/${scene.media.cache_path}`) ?? undefined}
                  controls
                  preload="metadata"
                  className="h-20 w-full bg-black object-cover"
                  aria-label={`Scene ${index + 1} video preview`}
                />
              ) : (
                /* Native img is intentional: cached media may be served by the API origin. */
                // eslint-disable-next-line @next/next/no-img-element
                <img
                  src={mediaUrl(`/media/${scene.media.cache_path}`) ?? undefined}
                  alt={`Scene ${index + 1}: ${scene.visual_goal}`}
                  loading="lazy"
                  className="h-20 w-full object-cover"
                />
              )}
            </div>
          ) : <div className="col-span-2 hidden sm:block" aria-hidden="true" />}
          <div className="col-span-2 flex items-center justify-between gap-3 text-right sm:col-span-1 sm:block">
            <button
              type="button"
              onClick={() => onChooseSceneMedia(index + 1)}
              disabled={mediaBusy !== null}
              className="media-action inline-flex items-center gap-1.5 rounded-full px-2.5 py-1.5 text-[9px] font-bold uppercase tracking-[.08em] sm:mb-2"
              aria-label={`Choose media for scene ${index + 1}`}
            >
              <RefreshCw className={cn("size-3", mediaBusy === index + 1 && "animate-spin")} />
              {mediaBusy === index + 1 ? "Loading…" : "Replace"}
            </button>
            <p className="mono text-[10px] font-medium">{formatTime(scene.start)}–{formatTime(scene.end)}</p>
            <p className="mt-1 text-[9px] uppercase tracking-[.08em] text-amber-700">{scene.asset_status.replaceAll("_", " ")}</p>
            {scene.visual_director?.generation?.status === "project_budget_exhausted" && <p className="mt-1 text-[9px] uppercase tracking-[.08em] text-amber-700">AI image budget reached</p>}
            {scene.overlays && scene.overlays.length > 0 && <p className="media-muted mt-1 text-[9px] uppercase tracking-[.08em]">+ {scene.overlays[0].kind} overlay</p>}
            {sceneQualityState(qualityReview, scene.id) === "repaired" && <p className="mt-1 text-[9px] uppercase tracking-[.08em] text-emerald-700">Auto-repaired</p>}
            {sceneQualityState(qualityReview, scene.id) === "issue" && <p className="mt-1 text-[9px] uppercase tracking-[.08em] text-amber-700">Quality issue</p>}
          </div>
          {candidateScene === index + 1 && candidateSet && (
            <div className="media-panel col-span-2 rounded-2xl p-3 sm:col-span-4">
              <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
                <p className="media-muted text-xs font-bold uppercase tracking-[.08em]">Alternatives · {candidateSet.preferred_kind} first</p>
                <div className="flex flex-wrap items-center gap-2">
                  <button type="button" onClick={() => onChooseSceneMedia(index + 1)} disabled={mediaBusy !== null} className="media-action inline-flex items-center gap-1.5 rounded-full px-3 py-1.5 text-xs font-semibold"><RefreshCw className="size-3" />Retry real media search</button>
                  <button type="button" onClick={onCancel} disabled={mediaBusy !== null} className="media-action rounded-full px-3 py-1.5 text-xs font-semibold">Keep current media</button>
                  {candidateSet.candidates.length > 0 && <button type="button" onClick={onApplyCandidate} disabled={!candidateSelection || mediaBusy !== null} className="media-primary rounded-full px-3 py-1.5 text-[10px] font-bold uppercase tracking-[.08em]">Apply</button>}
                </div>
              </div>
              {mediaError && <p role="alert" className="media-alert mb-3 rounded-xl px-3 py-2 text-xs">{mediaError}</p>}
              {candidateSet.candidates.filter((item) => !item.generated).length === 0 && <p role="status" className="media-note mb-3 rounded-xl px-3 py-2 text-sm leading-5">No suitable real media found for this scene. Retry the search, keep the current media, or generate an AI image below.</p>}
              <div className="grid gap-2 sm:grid-cols-3">
                {candidateSet.candidates.map((candidate) => (
                  <button key={candidate.token} type="button" onClick={() => onSelectCandidate(candidate.token)} aria-pressed={candidateSelection === candidate.token} className="media-candidate overflow-hidden rounded-xl text-left">
                    {candidateSelection === candidate.token && <span className="media-selected-badge"><Check className="size-3" aria-hidden="true" />Selected</span>}
                    {candidate.kind === "video" ? <video src={candidatePreview(candidate.preview_url)} muted controls preload="metadata" className="h-24 w-full bg-black object-cover" aria-label={`${candidate.kind} candidate preview`} /> : (
                      // eslint-disable-next-line @next/next/no-img-element
                      <img src={candidatePreview(candidate.preview_url)} alt={candidate.generated ? "AI-generated alternative" : `${candidate.provider} candidate by ${candidate.creator}`} loading="lazy" className="h-24 w-full object-cover" />
                    )}
                    <span className="block px-2 py-1.5 text-[10px] leading-4">
                      {candidate.generated
                        ? <><strong className="uppercase">{candidate.new ? "New · AI image" : "AI image"}</strong> · {candidate.model_label ?? "OpenAI"}<br /><span className="media-muted">{candidate.prompt_source === "user_edited" ? "Your prompt" : "Automatic prompt"}</span></>
                        : <><strong className="uppercase">{candidate.kind}</strong> · {candidate.provider}<br /><span className="media-muted">{candidate.creator}</span></>}
                    </span>
                  </button>
                ))}
              </div>
              {candidateSet.generation && (
                <div className="media-box mt-3 rounded-xl p-3">
                  <p className="media-muted mb-1.5 text-[10px] font-bold uppercase tracking-[.08em]">Generate AI image</p>
                  <GenerateImageOption key={candidateSet.generation.prompt} option={candidateSet.generation} busy={mediaBusy !== null} onGenerate={onGenerateImage} state={generationState} />
                </div>
              )}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

function SourcesView({ project }: { project: Project }) {
  const state = project.revision.state;
  if (!state.research.required) {
    return <EmptyState title="Story mode" body="This concept is fictional, so web research was intentionally skipped." />;
  }
  return (
    <div className="grid gap-4 md:grid-cols-2">
      <Panel icon={<Sparkles className="size-4" />} title="Research questions">
        <ol className="space-y-3">
          {state.research.questions.map((question, index) => <li key={question} className="flex gap-3 text-sm leading-5 text-[#56564e]"><span className="mono text-[10px] text-[#ff6838]">0{index + 1}</span>{question}</li>)}
        </ol>
        {state.research.error && <p className="mt-4 rounded-xl bg-red-50 p-3 text-xs text-red-700">{state.research.error}</p>}
      </Panel>
      <Panel icon={<Check className="size-4" />} title={`Fact pack · ${state.facts.length}`}>
        {state.facts.length ? <div className="space-y-3">{state.facts.map((fact) => <div key={fact.id} className="rounded-xl bg-[#faf9f4] p-3"><p className="text-xs leading-5 text-[#56564e]">{fact.claim}</p><p className="mono mt-2 text-[8px] uppercase text-[#aaa]">{fact.verification.replaceAll("_", " ")} · {Math.round(fact.confidence * 100)}%</p></div>)}</div> : <p className="text-sm leading-6 text-[#77776d]">No attributable facts are available yet.</p>}
      </Panel>
      <Panel icon={<ExternalLink className="size-4" />} title={`Sources · ${state.research.sources.length}`} wide>
        <div className="grid gap-2 sm:grid-cols-2">
          {state.research.sources.map((source: Source) => (
            <a key={source.url} href={source.url} target="_blank" rel="noreferrer" className="flex items-center justify-between gap-3 rounded-xl border border-black/7 bg-[#faf9f4] p-3 text-xs font-semibold text-[#4f4f48] hover:border-black/15">
              <span className="truncate">{source.label}</span><ExternalLink className="size-3 shrink-0 text-[#ff6838]" />
            </a>
          ))}
        </div>
      </Panel>
    </div>
  );
}

function ChatMessages({
  messages,
  loading,
  sending,
  project,
  compact = false,
}: {
  messages: ChatMessage[];
  loading: boolean;
  sending: boolean;
  project: Project;
  compact?: boolean;
}) {
  const endRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [messages, sending]);

  return (
    <div className={cn("flex-1 space-y-4 overflow-y-auto px-5 py-6", compact && "min-h-0 px-4 py-4")} aria-live="polite">
      {!loading && messages.length === 0 && (
        <AssistantMessage>
          I know this project’s script, scenes, voice, sources, timeline, and render state. Ask me a question or request a change.
        </AssistantMessage>
      )}
      {messages.map((item) => item.role === "user" ? (
        <div key={item.id} className="ml-auto max-w-[88%] whitespace-pre-wrap rounded-[18px] rounded-br-[5px] bg-[#1b1b18] px-4 py-3 text-sm leading-5 text-white">
          {item.content}
        </div>
      ) : (
        <div key={item.id}>
          <AssistantMessage>{item.content}</AssistantMessage>
          {item.tool_metadata.tools?.some((tool) => tool.mutating && tool.success) && (
            <p className="mono ml-10 mt-1.5 text-[8px] uppercase tracking-[.1em] text-[#99998f]">
              Project updated · v{item.tool_metadata.tools.findLast((tool) => tool.revision)?.revision ?? project.current_revision}
            </p>
          )}
        </div>
      ))}
      {(loading || sending) && (
        <div className="flex items-center gap-2 pl-1 text-xs font-medium text-[#88887f]">
          <LoaderCircle className="size-3.5 animate-spin text-[#ff6838]" />
          {loading ? "Reading conversation…" : "Reading project and working…"}
        </div>
      )}
      <div ref={endRef} />
    </div>
  );
}

function ChatError({ error, retry }: { error: string | null; retry: (() => void) | null }) {
  if (!error) return null;
  return (
    <div role="alert" className="mx-4 mb-2 flex items-center justify-between gap-3 rounded-xl bg-red-50 px-3 py-2 text-xs text-red-800">
      <span>{error}</span>
      {retry && <button onClick={retry} className="shrink-0 font-bold underline underline-offset-2">Retry</button>}
    </div>
  );
}

function Composer({ value, setValue, submit, busy, compact = false }: { value: string; setValue: (value: string) => void; submit: () => void; busy: boolean; compact?: boolean }) {
  return (
    <div className={cn("border-t border-black/8 p-4", compact && "border-0 p-0")}>
      <div className="cf-surface flex items-end gap-2 rounded-[20px] border p-2 shadow-sm transition-[border-color,box-shadow] duration-150 focus-within:border-[#ff6838]/35 focus-within:ring-4 focus-within:ring-[#ff6838]/5">
      <textarea aria-label="Project assistant message" rows={compact ? 1 : 3} value={value} onChange={(event) => setValue(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void submit(); } }} placeholder="Ask about this project or request a change…" className="min-h-10 flex-1 resize-none bg-transparent px-2 py-2 text-sm leading-5 outline-none placeholder:text-[#9d9d94]" />
        <Button variant="accent" size="icon" onClick={() => void submit()} disabled={value.trim().length < 1 || busy} aria-label="Send message">
          {busy ? <LoaderCircle className="size-4 animate-spin" /> : <Send className="size-4" />}
        </Button>
      </div>
      {!compact && <p className="mono mt-2 text-center text-[8px] uppercase tracking-[.08em] text-[#aaa]">Enter to send · Shift + Enter for line break</p>}
    </div>
  );
}

function AssistantMessage({ children, subtle = false }: { children: React.ReactNode; subtle?: boolean }) {
  return (
    <div className="flex gap-2.5">
      <div className={cn("mt-0.5 grid size-7 shrink-0 place-items-center rounded-lg bg-[#ff6838] text-white", subtle && "bg-[#dedbd0] text-[#75756c]")}><Sparkles className="size-3.5" /></div>
      <div className="cf-surface whitespace-pre-wrap rounded-[18px] rounded-tl-[5px] border px-4 py-3 text-[13px] leading-5 text-[var(--muted-foreground)] shadow-sm">{children}</div>
    </div>
  );
}

function Stat({ label, value, hint }: { label: string; value: string; hint: string }) {
  return <div className="cf-surface rounded-[16px] border p-3"><p className="mono text-[8px] uppercase tracking-[.12em] text-[var(--muted-foreground)]">{label}</p><p className="mt-1 text-xl font-semibold tracking-[-.04em]">{value}</p><p className="mt-0.5 truncate text-[9px] text-[var(--muted-foreground)]">{hint}</p></div>;
}

function Badge({ children }: { children: React.ReactNode }) {
  return <span className="cf-surface rounded-full border px-3 py-1.5 text-[10px] font-bold uppercase tracking-[.09em] text-[var(--muted-foreground)]">{children}</span>;
}

function Panel({ icon, title, children, wide = false }: { icon: React.ReactNode; title: string; children: React.ReactNode; wide?: boolean }) {
  return <div className={cn("cf-surface rounded-[20px] border p-5 shadow-sm", wide && "md:col-span-2")}><div className="mb-4 flex items-center gap-2 text-sm font-bold">{icon}<span>{title}</span></div>{children}</div>;
}

function OpeningHook({ hook }: { hook: TripleHookSummary | null }) {
  if (!hook) return null;
  const rows: Array<[string, string]> = [["Says", hook.verbal], ["Shows", hook.visual], ["Text", hook.onScreen], ...(hook.evidence ? [["Facts", hook.evidence] as [string, string]] : [])];
  return (
    <div className="cf-subtle rounded-xl border p-3 text-xs" aria-label="Opening hook">
      <div className="flex items-center justify-between gap-2"><span className="font-semibold">Opening hook</span><span className="mono rounded-full bg-black/[.04] px-2 py-0.5 text-[10px] font-bold" title="Documented hook strategy">{hook.strategy}</span></div>
      <dl className="mt-2 space-y-1">{rows.map(([name, value]) => <div key={name} className="flex gap-2"><dt className="w-10 shrink-0 text-[var(--muted-foreground)]">{name}</dt><dd className="min-w-0 break-words">{value}</dd></div>)}</dl>
      {hook.meta && <p className="mt-2 text-[10px] text-[var(--muted-foreground)]">{hook.meta}</p>}
    </div>
  );
}

function KeyValue({ label, value }: { label: string; value: string }) {
  return <div className="flex items-center justify-between gap-3 border-b border-black/6 pb-2.5 last:border-0 last:pb-0"><span className="text-[#818178]">{label}</span><span className="font-semibold capitalize text-[#34342f]">{value}</span></div>;
}

function EmptyState({ title, body }: { title: string; body: string }) {
  return <div className="cf-subtle rounded-[24px] border border-dashed px-6 py-12 text-center"><Film className="mx-auto size-6 text-[var(--muted-foreground)]" /><p className="mt-3 font-bold">{title}</p><p className="mx-auto mt-2 max-w-sm text-sm leading-6 text-[var(--muted-foreground)]">{body}</p></div>;
}

function formatTime(seconds: number) {
  const safe = Number.isFinite(seconds) ? Math.max(0, seconds) : 0;
  const minutes = Math.floor(safe / 60);
  const rest = Math.round(safe % 60);
  return `${minutes}:${String(rest).padStart(2, "0")}`;
}
