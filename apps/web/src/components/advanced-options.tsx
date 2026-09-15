"use client";

import * as Accordion from "@radix-ui/react-accordion";
import { ChevronDown, SlidersHorizontal } from "lucide-react";

type Options = Record<string, string | number | null>;

export function AdvancedOptions({ value, onChange }: { value: Options; onChange: (value: Options) => void }) {
  const update = (key: string, next: string | number | null) => onChange({ ...value, [key]: next === "" ? null : next });
  return (
    <Accordion.Root type="single" collapsible className="w-full">
      <Accordion.Item value="advanced">
        <Accordion.Trigger className="group mx-auto flex items-center gap-2 rounded-full px-3 py-2 text-sm font-medium text-[#77776d] transition-[transform,color,background-color] duration-150 ease-[cubic-bezier(.23,1,.32,1)] active:scale-[.97] hover:bg-black/5 hover:text-black">
          <SlidersHorizontal className="size-4" /> Advanced
          <ChevronDown className="size-4 transition-transform group-data-[state=open]:rotate-180" />
        </Accordion.Trigger>
        <Accordion.Content className="overflow-hidden">
          <div className="mt-3 grid grid-cols-2 gap-3 rounded-[24px] border border-black/8 bg-white/70 p-4 text-left shadow-sm backdrop-blur md:grid-cols-4">
            <Option label="Language" value={value.language as string} onChange={(v) => update("language", v)} options={["Auto", "English", "German"]} />
            <Option label="Style" value={value.style as string} onChange={(v) => update("style", v)} options={["Auto", "Documentary", "Cinematic", "Editorial"]} />
            <Option label="Voice" value={value.voice as string} onChange={(v) => update("voice", v)} options={["Auto", "Warm", "Serious", "Energetic"]} />
            <Option label="Format" value={value.aspect_ratio as string} onChange={(v) => update("aspect_ratio", v)} options={["9:16", "1:1", "16:9"]} />
            <Option label="Platform" value={value.platform as string} onChange={(v) => update("platform", v)} options={["Auto", "TikTok", "Instagram", "YouTube"]} />
            <Option label="Captions" value={value.caption_style as string} onChange={(v) => update("caption_style", v)} options={["Auto", "Bold clean", "Minimal", "Karaoke"]} />
            <Option label="Music" value={value.music as string} onChange={(v) => update("music", v)} options={["Auto", "Documentary pulse", "Cinematic", "Subtle"]} />
            <label className="space-y-1.5">
              <span className="ml-1 text-[11px] font-bold uppercase tracking-[0.12em] text-[#88887f]">Max seconds</span>
              <input
                type="number"
                min={10}
                max={180}
                value={value.max_duration ?? ""}
                placeholder="Auto"
                onChange={(event) => update("max_duration", event.target.value ? Number(event.target.value) : null)}
                className="h-10 w-full rounded-xl border border-black/10 bg-[#fafaf6] px-3 text-sm outline-none focus:border-[#ff6838]/50"
              />
            </label>
          </div>
        </Accordion.Content>
      </Accordion.Item>
    </Accordion.Root>
  );
}

function Option({ label, value, onChange, options }: { label: string; value?: string; onChange: (v: string) => void; options: string[] }) {
  return (
    <label className="space-y-1.5">
      <span className="ml-1 text-[11px] font-bold uppercase tracking-[0.12em] text-[#88887f]">{label}</span>
      <select value={value ?? ""} onChange={(event) => onChange(event.target.value)} className="h-10 w-full rounded-xl border border-black/10 bg-[#fafaf6] px-3 text-sm outline-none focus:border-[#ff6838]/50">
        {options.map((option) => <option key={option} value={option === "Auto" ? "" : option.toLowerCase()}>{option}</option>)}
      </select>
    </label>
  );
}
