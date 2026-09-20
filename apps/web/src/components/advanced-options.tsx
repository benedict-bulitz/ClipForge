"use client";

import * as Accordion from "@radix-ui/react-accordion";
import { Captions, ChevronDown, Film, Languages, Music2, RotateCcw, SlidersHorizontal, Sparkles } from "lucide-react";
import type { CreateOptions } from "@/lib/creation-preferences";
import { VoiceControls } from "./voice-controls";

export function AdvancedOptions({ value, onChange, prompt, onReset }: { value: CreateOptions; onChange: (value: CreateOptions) => void; prompt: string; onReset: () => void }) {
  const update = (key: string, next: string | number | boolean | null) => onChange({ ...value, [key]: next });
  const updateMaximum = (maximum: number) => onChange({
    ...value,
    max_duration: maximum,
    min_duration: typeof value.min_duration === "number" && value.min_duration > maximum ? maximum : value.min_duration,
  });
  return <Accordion.Root type="single" collapsible className="w-full"><Accordion.Item value="advanced">
    <div className="flex justify-center"><Accordion.Trigger className="group flex items-center gap-2 rounded-full px-3 py-2 text-sm font-medium text-[var(--muted-foreground)] transition hover:bg-[var(--surface-hover)] hover:text-[var(--foreground)]"><SlidersHorizontal className="size-4" /> Advanced settings <ChevronDown className="size-4 transition-transform group-data-[state=open]:rotate-180" /></Accordion.Trigger></div>
    <Accordion.Content className="overflow-hidden"><div className="cf-surface mt-3 space-y-3 rounded-[26px] border p-3 text-left shadow-sm sm:p-4">
      <div className="flex items-center justify-between gap-3 px-1 pb-1"><div><p className="text-sm font-bold">Creation controls</p><p className="mt-0.5 text-xs text-[var(--muted-foreground)]">Saved on this device for your next new project.</p></div><button type="button" onClick={onReset} className="interactive-text shrink-0"><RotateCcw className="size-3.5" /> Reset defaults</button></div>
      <SettingsGroup icon={<Film className="size-4" />} title="Video" description="Format, length, and pacing." initiallyOpen><div className="settings-grid">
        <Select label="Format" value={String(value.aspect_ratio)} onChange={(next) => update("aspect_ratio", next)} options={[["9:16", "Vertical 9:16"], ["1:1", "Square 1:1"], ["16:9", "Landscape 16:9"]]} />
        <label className="field-label"><span>Minimum length</span><input className="cf-input" type="number" min={10} max={Number(value.max_duration)} placeholder="Auto" value={value.min_duration === null ? "" : Number(value.min_duration)} onChange={(event) => update("min_duration", event.target.value === "" ? null : Number(event.target.value))} /><small>Auto uses the shortest complete answer.</small></label>
        <label className="field-label"><span>Maximum length</span><input className="cf-input" type="number" min={10} max={180} value={Number(value.max_duration)} onChange={(event) => updateMaximum(Number(event.target.value))} /><small>A ceiling, not a target. Default: 60 seconds.</small></label>
        <Select label="Cut speed" value={String(value.pacing)} onChange={(next) => update("pacing", next)} options={[["slow", "Relaxed"], ["balanced", "Balanced"], ["fast", "Fast"]]} />
        <p className="self-end pb-2 text-xs text-[var(--muted-foreground)]">How quickly scenes and visuals change. Narration speed stays separate.</p>
        <Select label="Platform" value={String(value.platform)} onChange={(next) => update("platform", next)} options={[["auto", "Automatic"], ["tiktok", "TikTok"], ["instagram", "Instagram"], ["youtube", "YouTube"]]} />
      </div></SettingsGroup>
      <SettingsGroup icon={<Languages className="size-4" />} title="Language & research" description="Auto follows the language of your prompt."><div className="settings-grid"><Select label="Language" value={String(value.language)} onChange={(next) => update("language", next)} options={[["auto", "Auto-detect"], ["en", "English"], ["de", "German"]]} /><Select label="Research" value={String(value.research)} onChange={(next) => update("research", next)} options={[["auto", "Automatic"], ["on", "Always research"], ["off", "Skip research"]]} /></div></SettingsGroup>
      <VoiceControls value={value} onChange={onChange} prompt={prompt} />
      <SettingsGroup icon={<Captions className="size-4" />} title="Captions" description="Small timed groups keep narration readable without covering footage."><div className="settings-grid">
        <Toggle label="Show captions" checked={Boolean(value.captions_enabled)} onChange={(next) => update("captions_enabled", next)} />
        <Select label="Style" value={String(value.caption_style)} onChange={(next) => update("caption_style", next)} options={[["clean", "Clean"], ["bold", "Bold"], ["minimal", "Minimal"], ["pop", "Pop"], ["boxed", "Boxed"], ["outline", "Outline"], ["karaoke", "Karaoke"]]} />
        <Select label="Position" value={String(value.caption_position)} onChange={(next) => update("caption_position", next)} options={[["upper", "Upper"], ["center", "Center"], ["lower", "Lower"]]} />
        <Range label={`Font size · ${value.caption_font_size}px`} min={32} max={112} step={2} value={Number(value.caption_font_size)} onChange={(next) => update("caption_font_size", next)} />
        <ColorControl label="Text color" value={String(value.caption_text_color)} onChange={(next) => update("caption_text_color", next)} />
        <ColorControl label="Active word" value={String(value.caption_highlight_color)} onChange={(next) => update("caption_highlight_color", next)} />
        <Range label={`Visible words · ${value.caption_words_per_group}`} min={2} max={8} step={1} value={Number(value.caption_words_per_group)} onChange={(next) => update("caption_words_per_group", next)} />
      </div><CaptionPreview options={value} /></SettingsGroup>
      <SettingsGroup icon={<Music2 className="size-4" />} title="Music" description="A locally generated original tone bed is mixed under narration."><div className="settings-grid">
        <Toggle label="Music bed" checked={Boolean(value.music_enabled)} onChange={(next) => update("music_enabled", next)} />
        <Select disabled={!value.music_enabled} label="Mood" value={String(value.music_mood)} onChange={(next) => update("music_mood", next)} options={[["ambient", "Ambient"], ["documentary", "Documentary"], ["tech", "Tech"], ["cinematic", "Cinematic"]]} />
        <Range disabled={!value.music_enabled} label={`Volume · ${Math.round(Number(value.music_volume) * 100)}%`} min={0} max={0.5} step={0.01} value={Number(value.music_volume)} onChange={(next) => update("music_volume", next)} />
        <Toggle disabled={!value.music_enabled} label="Lower under narration" checked={Boolean(value.music_ducking)} onChange={(next) => update("music_ducking", next)} />
        <Toggle disabled={!value.music_enabled} label="Fade in and out" checked={Boolean(value.music_fades)} onChange={(next) => update("music_fades", next)} />
      </div></SettingsGroup>
      <SettingsGroup icon={<Sparkles className="size-4" />} title="Visual direction" description="Controls the overall editorial treatment."><div className="settings-grid"><Select label="Style" value={String(value.style)} onChange={(next) => update("style", next)} options={[["documentary", "Documentary"], ["cinematic", "Cinematic"], ["editorial", "Editorial"]]} /><Select label="Visual activity" value={String(value.attention_density)} onChange={(next) => update("attention_density", next)} options={[["off", "Off"], ["low", "Relaxed"], ["normal", "Normal"], ["high", "Frequent"], ["custom", "Custom"]]} />{value.attention_density === "custom" ? <label className="field-label"><span>Approx. every {value.attention_interval_seconds}s</span><input className="cf-input" type="number" min={0.6} max={8} step={0.1} value={Number(value.attention_interval_seconds)} onChange={(event) => update("attention_interval_seconds", Number(event.target.value))} /><small>Adds short callouts and emphasis between cuts. Background movement does not count.</small></label> : <p className="self-end pb-2 text-xs text-[var(--muted-foreground)]">Adds short callouts and emphasis between cuts. Background movement does not count.</p>}</div></SettingsGroup>
    </div></Accordion.Content>
  </Accordion.Item></Accordion.Root>;
}

function SettingsGroup({ icon, title, description, children, initiallyOpen = false }: { icon: React.ReactNode; title: string; description: string; children: React.ReactNode; initiallyOpen?: boolean }) {
  return <details className="cf-subtle group rounded-[20px] border p-4" open={initiallyOpen}><summary className="flex cursor-pointer list-none items-center gap-3"><span className="grid size-8 place-items-center rounded-xl bg-[var(--accent-soft)] text-[var(--accent)]">{icon}</span><span className="min-w-0 flex-1"><strong className="block text-sm">{title}</strong><span className="block text-xs font-normal text-[var(--muted-foreground)]">{description}</span></span><ChevronDown className="size-4 text-[var(--muted-foreground)] transition-transform group-open:rotate-180" /></summary><div className="mt-4 border-t border-[var(--border)] pt-4">{children}</div></details>;
}

function Select({ label, value, onChange, options, disabled = false }: { label: string; value: string; onChange: (value: string) => void; options: string[][]; disabled?: boolean }) { return <label className="field-label"><span>{label}</span><select disabled={disabled} value={value} onChange={(event) => onChange(event.target.value)} className="cf-input">{options.map(([key, text]) => <option key={key} value={key}>{text}</option>)}</select></label>; }
function Toggle({ label, checked, onChange, disabled = false }: { label: string; checked: boolean; onChange: (value: boolean) => void; disabled?: boolean }) { return <label className="flex min-h-10 items-center justify-between gap-3 rounded-xl border border-[var(--border)] bg-[var(--input)] px-3 text-xs font-semibold"><span>{label}</span><input type="checkbox" disabled={disabled} checked={checked} onChange={(event) => onChange(event.target.checked)} className="size-4 accent-[var(--accent)]" /></label>; }
function Range({ label, value, min, max, step, onChange, disabled = false }: { label: string; value: number; min: number; max: number; step: number; onChange: (value: number) => void; disabled?: boolean }) { return <label className="field-label"><span>{label}</span><input type="range" disabled={disabled} min={min} max={max} step={step} value={value} onChange={(event) => onChange(Number(event.target.value))} className="h-10 w-full accent-[var(--accent)]" /></label>; }
function ColorControl({ label, value, onChange }: { label: string; value: string; onChange: (value: string) => void }) { return <label className="field-label"><span>{label}</span><span className="cf-input flex items-center gap-2"><input type="color" value={value} onChange={(event) => onChange(event.target.value)} className="size-7 cursor-pointer border-0 bg-transparent p-0" /><span className="font-mono text-xs">{value.toUpperCase()}</span></span></label>; }

function CaptionPreview({ options }: { options: CreateOptions }) {
  const visible = ["Why", "does", "our", "planet", "change", "today"].slice(0, Number(options.caption_words_per_group));
  return <div className="mt-4"><p className="mb-2 text-[10px] font-bold uppercase tracking-[.12em] text-[var(--muted-foreground)]">Live caption preview</p><div className={`caption-preview caption-preview-${options.caption_style} caption-position-${options.caption_position}`}><div style={{ fontSize: `${Math.max(13, Number(options.caption_font_size) * 0.24)}px`, color: String(options.caption_text_color) }}>{options.captions_enabled ? visible.map((word, index) => <span key={word} style={index === Math.min(3, visible.length - 1) ? { color: String(options.caption_highlight_color) } : undefined}>{word}{index < visible.length - 1 ? " " : ""}</span>) : <span className="text-sm font-medium normal-case text-white/70">Captions are off</span>}</div></div></div>;
}
