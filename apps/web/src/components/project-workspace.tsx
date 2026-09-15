"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import {
  ArrowLeft,
  Check,
  CircleDot,
  Download,
  ExternalLink,
  Film,
  History,
  Layers3,
  LoaderCircle,
  MessageSquareText,
  MoreHorizontal,
  RotateCcw,
  Send,
  Sparkles,
  WandSparkles,
} from "lucide-react";
import {
  editProjectAtRevision,
  getReadiness,
  mediaUrl,
  renderProject,
  undoProject,
} from "@/lib/api";
import type { Project, Readiness, Scene, Source } from "@/lib/types";
import { Brand } from "./brand";
import { Button } from "./ui/button";
import { cn } from "@/lib/utils";

type Tab = "overview" | "script" | "scenes" | "sources";

const setupLinks: Record<string, { label: string; url: string }> = {
  director: { label: "Get OpenAI key", url: "https://platform.openai.com/api-keys" },
  research: { label: "Get Brave key", url: "https://api-dashboard.search.brave.com/app/keys" },
  media: { label: "Get Pexels key", url: "https://www.pexels.com/api/new/" },
  voice: { label: "Voice setup", url: "https://platform.openai.com/api-keys" },
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
  const [tab, setTab] = useState<Tab>("overview");
  const [instruction, setInstruction] = useState("");
  const [busy, setBusy] = useState<"edit" | "render" | "undo" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [moreOpen, setMoreOpen] = useState(false);
  const [readiness, setReadiness] = useState<Readiness | null>(null);
  const state = project.revision.state;
  const duration = state.duration.actual_seconds ?? state.duration.estimated_seconds;
  const downloadUrl = mediaUrl(state.render.url);

  useEffect(() => {
    let active = true;
    getReadiness()
      .then((next) => { if (active) setReadiness(next); })
      .catch(() => undefined);
    return () => { active = false; };
  }, [project.current_revision]);

  async function submitEdit() {
    if (instruction.trim().length < 2 || busy) return;
    setBusy("edit");
    setError(null);
    try {
      const next = await editProjectAtRevision(project.id, instruction, project.current_revision);
      onProjectChange(next);
      setInstruction("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The edit could not be applied.");
    } finally {
      setBusy(null);
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

  async function undo() {
    if (busy || project.current_revision <= 1) return;
    setBusy("undo");
    setError(null);
    try {
      onProjectChange(await undoProject(project.id, project.current_revision));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Nothing to undo.");
    } finally {
      setBusy(null);
    }
  }

  return (
    <main className="min-h-screen bg-[#eeece4]">
      <div className="noise" />
      <header className="sticky top-0 z-40 border-b border-black/8 bg-[#f4f2ea]/92 backdrop-blur-xl">
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
            <CircleDot className="size-3" /> {state.render.status === "complete" ? "Rendered" : "Ready"}
          </div>
          <button onClick={() => void undo()} disabled={!!busy || project.current_revision <= 1} className="interactive-icon lg:hidden" aria-label="Undo latest edit">
            <RotateCcw className="size-4" />
          </button>
          {downloadUrl ? (
            <Button asChild variant="outline" size="sm">
              <a href={downloadUrl} download><Download className="size-3.5" /><span className="hidden sm:inline">Export MP4</span></a>
            </Button>
          ) : (
            <Button variant="accent" size="sm" onClick={() => void render()} disabled={!!busy || state.render.status === "blocked_by_research"}>
              {busy === "render" ? <LoaderCircle className="size-3.5 animate-spin" /> : <Film className="size-3.5" />}
              <span className="hidden sm:inline">Render video</span>
            </Button>
          )}
          <Button variant="ghost" size="icon" aria-label="Project history" aria-expanded={moreOpen} onClick={() => setMoreOpen((open) => !open)}>
            <MoreHorizontal className="size-5" />
          </Button>
          {moreOpen && (
            <div className="absolute right-3 top-[calc(100%+.4rem)] z-50 w-[min(330px,calc(100vw-1.5rem))] origin-top-right rounded-[18px] border border-black/10 bg-[#fbfaf5] p-3 shadow-[0_18px_55px_rgba(30,27,17,.18)]">
              <div className="mb-2 flex items-center gap-2 px-2 py-1 text-xs font-bold"><History className="size-3.5 text-[#ff6838]" /> Revision history</div>
              <div className="max-h-64 space-y-1 overflow-y-auto">
                {project.revisions.map((revision) => (
                  <div key={revision.id} className={cn("rounded-xl px-3 py-2 text-xs", revision.is_current ? "bg-[#ff6838]/10" : "bg-black/[.025]")}>
                    <div className="flex items-center justify-between gap-2">
                      <span className="font-semibold">v{revision.number}</span>
                      {revision.is_current && <span className="text-[9px] font-bold uppercase text-[#d94c20]">Current</span>}
                    </div>
                    <p className="mt-1 truncate text-[#77776d]">{revision.instruction}</p>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      </header>

      <div className="mx-auto grid max-w-[1600px] grid-cols-1 lg:grid-cols-[minmax(560px,1fr)_390px]">
        <section className="min-w-0 border-black/8 px-4 pb-32 pt-6 lg:border-r lg:px-8 lg:pb-10">
          <div className="mx-auto max-w-[1040px]">
            {error && <div role="alert" className="mb-5 rounded-[16px] border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-800">{error}</div>}
            <div className="grid items-start gap-7 md:grid-cols-[minmax(260px,420px)_minmax(0,1fr)]">
              <VideoPreview project={project} />
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
                <Pipeline stages={state.pipeline} />
              </div>
            </div>

            <div className="mt-8 border-b border-black/10">
              <nav className="flex gap-1 overflow-x-auto" aria-label="Project detail tabs">
                {(["overview", "script", "scenes", "sources"] as Tab[]).map((item) => (
                  <button key={item} onClick={() => setTab(item)} className={cn(
                    "relative px-4 py-3 text-sm font-semibold capitalize text-[#818177] transition-colors duration-150 ease-[cubic-bezier(.23,1,.32,1)] hover:text-black",
                    tab === item && "text-black after:absolute after:inset-x-4 after:bottom-[-1px] after:h-0.5 after:rounded-full after:bg-[#ff6838]",
                  )}>{item}</button>
                ))}
              </nav>
            </div>
            <div className="py-6">
              {tab === "overview" && <Overview project={project} readiness={readiness} />}
              {tab === "script" && <ScriptView project={project} />}
              {tab === "scenes" && <ScenesView scenes={state.scenes} duration={duration} />}
              {tab === "sources" && <SourcesView project={project} />}
            </div>
          </div>
        </section>

        <aside className="fixed inset-x-0 bottom-0 z-30 border-t border-black/10 bg-[#f7f5ee]/95 p-3 shadow-[0_-10px_35px_rgba(40,35,20,.08)] backdrop-blur-xl lg:sticky lg:top-16 lg:h-[calc(100vh-4rem)] lg:border-t-0 lg:bg-[#f7f5ee]/70 lg:p-0 lg:shadow-none">
          <div className="hidden h-full flex-col lg:flex">
            <div className="border-b border-black/8 px-6 py-5">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2 text-sm font-bold"><MessageSquareText className="size-4 text-[#ff6838]" /> Direct this video</div>
                <button onClick={() => void undo()} disabled={!!busy || project.current_revision <= 1} className="interactive-text">
                  <RotateCcw className="size-3.5" /> Undo
                </button>
              </div>
              <p className="mt-2 text-xs leading-5 text-[#88887f]">Describe changes to script, captions, voice, music, visuals, language, or format.</p>
            </div>
            <div className="flex-1 space-y-4 overflow-y-auto px-5 py-6">
              <AssistantMessage>Project v{project.current_revision} has {state.scenes.length} visual beats and a {formatTime(duration)} narration.</AssistantMessage>
              {state.render.status !== "complete" && (
                <AssistantMessage subtle>Edits invalidate the existing render. Use Render video when the project is ready.</AssistantMessage>
              )}
              {state.edit_history.map((edit) => (
                <div key={`${edit.revision}-${edit.created_at}`} className="space-y-3">
                  <div className="ml-auto max-w-[88%] rounded-[18px] rounded-br-[5px] bg-[#1b1b18] px-4 py-3 text-sm leading-5 text-white">{edit.instruction}</div>
                  <AssistantMessage>Applied {edit.summary}. Saved in project revision {edit.revision}.</AssistantMessage>
                </div>
              ))}
            </div>
            <Composer value={instruction} setValue={setInstruction} submit={submitEdit} busy={busy === "edit"} />
          </div>
          <div className="lg:hidden"><Composer value={instruction} setValue={setInstruction} submit={submitEdit} busy={busy === "edit"} compact /></div>
        </aside>
      </div>
    </main>
  );
}

function VideoPreview({ project }: { project: Project }) {
  const state = project.revision.state;
  const hook = state.script.blocks[0]?.text ?? project.original_prompt;
  const source = mediaUrl(state.render.url);
  const vertical = state.timeline.height > state.timeline.width;
  const label = source ? "Rendered preview" : "Storyboard preview";
  return (
    <div className="mx-auto w-full" style={{ maxWidth: vertical ? 292 : 520 }}>
      <div
        className={cn(
          "relative overflow-hidden border-[6px] border-[#1c1c19] bg-[#12120f] shadow-[0_25px_60px_rgba(30,27,17,.26)]",
          vertical ? "rounded-[30px]" : "rounded-[22px]",
        )}
        style={{ aspectRatio: `${state.timeline.width} / ${state.timeline.height}` }}
      >
        {source ? (
          <video key={source} src={source} controls playsInline preload="metadata" className="size-full bg-black object-contain" aria-label="ClipForge video preview" />
        ) : (
          <>
            <div className="absolute inset-0 bg-[radial-gradient(circle_at_66%_22%,#ffb178_0,transparent_22%),radial-gradient(circle_at_30%_70%,#253449_0,transparent_30%),linear-gradient(155deg,#7d2a16_0%,#191914_45%,#080809_100%)]" />
            <div className="absolute inset-0 bg-gradient-to-b from-black/10 via-transparent to-black/75" />
            <div className="absolute left-4 right-4 top-4 flex items-center justify-between text-[8px] font-bold uppercase tracking-[.13em] text-white/75">
              <span>ClipForge / Preview</span><span>{state.timeline.aspect_ratio}</span>
            </div>
            <div className="absolute inset-x-4 bottom-[21%] text-center">
              <p
                className="font-extrabold uppercase leading-[1.02] tracking-[-.055em] drop-shadow-lg"
                style={{ fontSize: Math.max(16, state.captions.font_size * 0.3), color: state.captions.highlight_color ?? "#ffffff" }}
              >
                {hook.split(" ").slice(0, 11).join(" ")}
              </p>
              <span className="mt-2 inline-block h-1 w-12 rounded-full bg-[#ff6838]" />
            </div>
          </>
        )}
      </div>
      <p className="mt-3 text-center text-[10px] font-semibold uppercase tracking-[.12em] text-[#88887f]">{label} · {state.timeline.aspect_ratio}</p>
    </div>
  );
}

function Pipeline({ stages }: { stages: Project["revision"]["state"]["pipeline"] }) {
  return (
    <div className="mt-6 rounded-[20px] border border-black/8 bg-white/55 p-4 shadow-sm">
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

function Overview({ project, readiness }: { project: Project; readiness: Readiness | null }) {
  const state = project.revision.state;
  const rows = readiness
    ? Object.entries(readiness)
    : Object.entries(state.integrations).map(([name, status]) => [name, { ready: !status.includes("unavailable"), status, key: null, url: setupLinks[name]?.url ?? "#" }] as const);
  return (
    <div className="grid gap-4 md:grid-cols-2">
      <Panel icon={<WandSparkles className="size-4" />} title="Creative direction">
        <div className="space-y-3 text-sm">
          <KeyValue label="Story type" value={state.intent.content_type.replaceAll("_", " ")} />
          <KeyValue label="Tone" value={state.intent.tone.replaceAll("_", " ")} />
          <KeyValue label="Voice" value={state.voice.profile.replaceAll("_", " ")} />
          <KeyValue label="Music" value={state.music.mood.replaceAll("_", " ")} />
          <KeyValue label="Cut pace" value={state.timeline.cut_pace} />
        </div>
      </Panel>
      <Panel icon={<Layers3 className="size-4" />} title="Build readiness">
        <div className="space-y-3">
          {rows.map(([name, item]) => {
            const link = setupLinks[name] ?? { label: "Setup", url: item.url };
            return (
              <div key={name} className="rounded-xl border border-black/6 bg-[#faf9f4] p-3 text-xs">
                <div className="flex items-center justify-between gap-3">
                  <span className="font-semibold capitalize text-[#4f4f48]">{name.replaceAll("_", " ")}</span>
                  <span className={cn("rounded-full px-2 py-1 font-bold", item.ready ? "bg-emerald-50 text-emerald-700" : "bg-amber-50 text-amber-700")}>{item.status.replaceAll("_", " ")}</span>
                </div>
                <div className="mt-2 flex items-center justify-between gap-2 text-[10px] text-[#8b8b82]">
                  <span className="mono truncate">{item.key ?? "No key required"}</span>
                  <a href={link.url} target="_blank" rel="noreferrer" className="inline-flex shrink-0 items-center gap-1 font-bold text-[#d94c20] hover:text-[#a93210]">
                    {link.label} <ExternalLink className="size-2.5" />
                  </a>
                </div>
              </div>
            );
          })}
          <p className="px-1 text-[10px] leading-4 text-[#898980]">Put keys in the root <span className="mono">.env</span> file, then restart the API.</p>
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

function ScriptView({ project }: { project: Project }) {
  const blocks = project.revision.state.script.blocks;
  return (
    <Panel icon={<MessageSquareText className="size-4" />} title={`Narration · ${project.revision.state.script.word_count} words`}>
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

function ScenesView({ scenes, duration }: { scenes: Scene[]; duration: number }) {
  return (
    <div className="space-y-3">
      <div className="relative mb-6 flex h-16 overflow-hidden rounded-[16px] border border-black/8 bg-white p-1.5 shadow-sm">
        {scenes.map((scene, index) => (
          <div key={scene.id} title={scene.narration} style={{ width: `${((scene.end - scene.start) / Math.max(1, duration)) * 100}%` }} className={cn("relative min-w-[5%] overflow-hidden border-r border-white/50 last:border-0", ["bg-[#ff8b62]", "bg-[#2f4054]", "bg-[#dbb164]", "bg-[#8b9c77]"][index % 4])}>
            <span className="mono absolute bottom-1.5 left-2 text-[8px] text-white/80">{index + 1}</span>
          </div>
        ))}
      </div>
      {scenes.map((scene, index) => (
        <div key={scene.id} className="grid grid-cols-[46px_minmax(0,1fr)] items-center gap-3 rounded-[18px] border border-black/8 bg-white/60 p-3 shadow-sm sm:grid-cols-[54px_minmax(0,1fr)_auto] sm:gap-4">
          <div className={cn("grid aspect-square place-items-center rounded-xl text-sm font-extrabold text-white", ["bg-[#ff7950]", "bg-[#34475d]", "bg-[#c89941]", "bg-[#7c8f67]"][index % 4])}>{index + 1}</div>
          <div className="min-w-0">
            <p className="truncate text-sm font-semibold">{scene.visual_goal}</p>
            <p className="mt-1 truncate text-xs text-[#84847b]">{scene.narration}</p>
          </div>
          <div className="col-span-2 flex justify-between text-right sm:col-span-1 sm:block">
            <p className="mono text-[10px] font-medium">{formatTime(scene.start)}–{formatTime(scene.end)}</p>
            <p className="mt-1 text-[9px] uppercase tracking-[.08em] text-amber-700">{scene.asset_status.replaceAll("_", " ")}</p>
          </div>
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

function Composer({ value, setValue, submit, busy, compact = false }: { value: string; setValue: (value: string) => void; submit: () => void; busy: boolean; compact?: boolean }) {
  return (
    <div className={cn("border-t border-black/8 p-4", compact && "border-0 p-0")}>
      <div className="flex items-end gap-2 rounded-[20px] border border-black/10 bg-white p-2 shadow-sm transition-[border-color,box-shadow] duration-150 focus-within:border-[#ff6838]/35 focus-within:ring-4 focus-within:ring-[#ff6838]/5">
        <textarea rows={compact ? 1 : 3} value={value} onChange={(event) => setValue(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void submit(); } }} placeholder="Ask ClipForge to change anything…" className="min-h-10 flex-1 resize-none bg-transparent px-2 py-2 text-sm leading-5 outline-none placeholder:text-[#9d9d94]" />
        <Button variant="accent" size="icon" onClick={() => void submit()} disabled={value.trim().length < 2 || busy} aria-label="Send edit">
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
      <div className="rounded-[18px] rounded-tl-[5px] border border-black/7 bg-white/75 px-4 py-3 text-[13px] leading-5 text-[#5d5d55] shadow-sm">{children}</div>
    </div>
  );
}

function Stat({ label, value, hint }: { label: string; value: string; hint: string }) {
  return <div className="rounded-[16px] border border-black/8 bg-white/50 p-3"><p className="mono text-[8px] uppercase tracking-[.12em] text-[#929289]">{label}</p><p className="mt-1 text-xl font-semibold tracking-[-.04em]">{value}</p><p className="mt-0.5 truncate text-[9px] text-[#99998f]">{hint}</p></div>;
}

function Badge({ children }: { children: React.ReactNode }) {
  return <span className="rounded-full border border-black/8 bg-white/60 px-3 py-1.5 text-[10px] font-bold uppercase tracking-[.09em] text-[#717168]">{children}</span>;
}

function Panel({ icon, title, children, wide = false }: { icon: React.ReactNode; title: string; children: React.ReactNode; wide?: boolean }) {
  return <div className={cn("rounded-[20px] border border-black/8 bg-white/60 p-5 shadow-sm", wide && "md:col-span-2")}><div className="mb-4 flex items-center gap-2 text-sm font-bold">{icon}<span>{title}</span></div>{children}</div>;
}

function KeyValue({ label, value }: { label: string; value: string }) {
  return <div className="flex items-center justify-between gap-3 border-b border-black/6 pb-2.5 last:border-0 last:pb-0"><span className="text-[#818178]">{label}</span><span className="font-semibold capitalize text-[#34342f]">{value}</span></div>;
}

function EmptyState({ title, body }: { title: string; body: string }) {
  return <div className="rounded-[24px] border border-dashed border-black/15 bg-white/35 px-6 py-12 text-center"><Film className="mx-auto size-6 text-[#aaa]" /><p className="mt-3 font-bold">{title}</p><p className="mx-auto mt-2 max-w-sm text-sm leading-6 text-[#77776d]">{body}</p></div>;
}

function formatTime(seconds: number) {
  const safe = Number.isFinite(seconds) ? Math.max(0, seconds) : 0;
  const minutes = Math.floor(safe / 60);
  const rest = Math.round(safe % 60);
  return `${minutes}:${String(rest).padStart(2, "0")}`;
}
