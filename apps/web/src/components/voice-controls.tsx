"use client";

import { useEffect, useRef, useState } from "react";
import { AudioLines, LoaderCircle, Volume2 } from "lucide-react";
import { mediaUrl, previewVoice } from "@/lib/api";
import { resolvePreviewLanguage, type CreateOptions } from "@/lib/creation-preferences";
import type { VoiceId, VoicePresentation, VoicePreviewRequest, VoiceTone } from "@/lib/types";

const voices: Array<{ id: VoiceId; label: string; description: string }> = [
  { id: "marin", label: "Marin", description: "Natural and clear" },
  { id: "cedar", label: "Cedar", description: "Grounded and direct" },
  { id: "coral", label: "Coral", description: "Bright and expressive" },
  { id: "onyx", label: "Onyx", description: "Deep and steady" },
  { id: "sage", label: "Sage", description: "Calm and measured" },
  { id: "verse", label: "Verse", description: "Conversational" },
];

const tones: Array<{ value: VoiceTone; label: string }> = [
  { value: "warm", label: "Warm" },
  { value: "calm", label: "Calm" },
  { value: "energetic", label: "Energetic" },
  { value: "deep", label: "Deep" },
  { value: "documentary", label: "Documentary" },
  { value: "conversational", label: "Conversational" },
];

export function VoiceControls({
  value,
  onChange,
  prompt,
}: {
  value: CreateOptions;
  onChange: (value: CreateOptions) => void;
  prompt: string;
}) {
  const voiceId = (value.voice_id as VoiceId | null) ?? "marin";
  const presentation = (value.voice_presentation as VoicePresentation | null) ?? "neutral";
  const tone = (value.voice_tone as VoiceTone | null) ?? "warm";
  const speed = typeof value.voice_speed === "number" ? value.voice_speed : 1;
  const language = resolvePreviewLanguage(prompt, value.language);
  const signature = JSON.stringify({
    voiceId,
    presentation,
    tone,
    speed,
    languageChoice: value.language ?? "auto",
    resolvedLanguage: language,
  });
  const [preview, setPreview] = useState<{ signature: string; url: string; cached: boolean; provider: "openai" | "macos_say" } | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [autoplayBlocked, setAutoplayBlocked] = useState(false);
  const audioRef = useRef<HTMLAudioElement>(null);
  const promptRef = useRef(prompt);
  const previousSettingsRef = useRef({ signature, speed });
  const requestRef = useRef(0);

  useEffect(() => {
    promptRef.current = prompt;
  }, [prompt]);

  useEffect(() => {
    const previous = previousSettingsRef.current;
    if (previous.signature === signature) return;
    previousSettingsRef.current = { signature, speed };
    const requestId = ++requestRef.current;
    const controller = new AbortController();
    const delay = previous.speed !== speed ? 800 : 400;
    setLoading(true);
    setError(null);
    const timer = window.setTimeout(() => {
      const payload: VoicePreviewRequest = {
        text: promptRef.current.trim() ? promptRef.current.trim().slice(0, 220) : undefined,
        language,
        voice_id: voiceId,
        presentation,
        tone,
        speed,
      };
      previewVoice(payload, controller.signal)
        .then((result) => {
          if (requestRef.current !== requestId) return;
          const url = mediaUrl(result.url);
          if (!url) throw new Error("The preview audio was not available.");
          setPreview({ signature, url, cached: result.cached, provider: result.provider });
          setAutoplayBlocked(false);
          requestAnimationFrame(() => {
            if (requestRef.current !== requestId) return;
            audioRef.current?.play().catch(() => {
              if (requestRef.current === requestId) setAutoplayBlocked(true);
            });
          });
        })
        .catch((reason: unknown) => {
          if (controller.signal.aborted || requestRef.current !== requestId) return;
          setError(reason instanceof Error ? reason.message : "Voice preview could not be generated.");
        })
        .finally(() => {
          if (requestRef.current === requestId) setLoading(false);
        });
    }, delay);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [language, presentation, signature, speed, tone, voiceId]);

  function update(key: string, next: string | number) {
    onChange({ ...value, [key]: next });
  }

  const outdated = Boolean(preview && preview.signature !== signature);
  const selectedVoice = voices.find((voice) => voice.id === voiceId)?.label ?? voiceId;
  return (
    <section className="cf-surface rounded-[24px] border p-4 text-left shadow-sm backdrop-blur md:p-5" aria-labelledby="voice-heading">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex gap-3">
          <div className="grid size-9 shrink-0 place-items-center rounded-xl bg-[#ff6838]/10 text-[#e65328]"><AudioLines className="size-4" /></div>
          <div>
            <h2 id="voice-heading" className="text-sm font-bold">Narrator voice</h2>
            <p className="mt-1 text-xs leading-5 text-[var(--muted-foreground)]">Choose how the narration should sound. The permanent player updates automatically after changes settle.</p>
          </div>
        </div>
        {loading && <span role="status" className="inline-flex items-center gap-2 text-xs font-semibold text-[var(--muted-foreground)]"><LoaderCircle className="size-3.5 animate-spin" /> Preparing preview…</span>}
      </div>

      <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Select label="Voice" value={voiceId} onChange={(next) => update("voice_id", next)}>
          {voices.map((voice) => <option key={voice.id} value={voice.id}>{voice.label} · {voice.description}</option>)}
        </Select>
        <Select label="Presentation" value={presentation} onChange={(next) => update("voice_presentation", next)}>
          <option value="neutral">Neutral</option>
          <option value="masculine">Masculine</option>
          <option value="feminine">Feminine</option>
        </Select>
        <Select label="Tone" value={tone} onChange={(next) => update("voice_tone", next)}>
          {tones.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
        </Select>
        <label className="space-y-1.5">
          <span className="ml-1 text-[10px] font-bold uppercase tracking-[.12em] text-[var(--muted-foreground)]">Speed · {speed.toFixed(2)}×</span>
          <input
            type="range"
            min="0.7"
            max="1.4"
            step="0.05"
            value={speed}
            onChange={(event) => update("voice_speed", Number(event.target.value))}
            className="h-10 w-full accent-[#ff6838]"
            aria-label="Narrator speaking speed"
          />
        </label>
      </div>

      <div className="cf-subtle mt-4 rounded-2xl border p-3">
        <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
          <span className="flex items-center gap-2 text-xs font-bold"><Volume2 className="size-4 text-[#ff6838]" /> Voice player</span>
          <span className="mono text-[8px] uppercase tracking-[.1em] text-[var(--muted-foreground)]">
            {selectedVoice} · {tone} · {speed.toFixed(2)}× · {language.toUpperCase()}
          </span>
        </div>
        <audio ref={audioRef} controls preload="metadata" src={preview?.url} className="h-10 w-full" aria-label="Voice preview player" />
        <p className="mt-2 text-[11px] text-[var(--muted-foreground)]">
          {!preview ? "Change a voice setting to prepare a short sample." : loading || outdated ? "Preparing the updated preview. The current sample remains playable." : autoplayBlocked ? "Preview is ready. Your browser blocked autoplay; press Play to listen." : preview.cached ? `Playing a cached ${preview.provider === "openai" ? "OpenAI" : "macOS system fallback"} voice sample.` : `${preview.provider === "openai" ? "OpenAI" : "macOS system fallback"} voice preview is ready.`}
        </p>
      </div>
      {error && <p role="alert" className="mt-3 text-xs font-medium text-[var(--destructive)]">{error}</p>}
    </section>
  );
}

function Select({ label, value, onChange, children }: { label: string; value: string; onChange: (value: string) => void; children: React.ReactNode }) {
  return (
    <label className="space-y-1.5">
      <span className="ml-1 text-[10px] font-bold uppercase tracking-[.12em] text-[var(--muted-foreground)]">{label}</span>
      <select value={value} onChange={(event) => onChange(event.target.value)} className="cf-input text-xs">
        {children}
      </select>
    </label>
  );
}
