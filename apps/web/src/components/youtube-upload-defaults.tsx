"use client";

import { useEffect, useState } from "react";
import { LoaderCircle, Save } from "lucide-react";
import { getYouTubeUploadDefaults, listYouTubeCategories, saveYouTubeUploadDefaults } from "@/lib/api";
import { browserLocale, detectTimeZone, regionFromLocale, zoneLabel, type UploadDefaults, type Visibility } from "@/lib/youtube";
import { Button } from "./ui/button";

type TriState = "ask" | "no" | "yes";

const toTri = (value: boolean | null): TriState => (value === null ? "ask" : value ? "yes" : "no");
const fromTri = (value: TriState): boolean | null => (value === "ask" ? null : value === "yes");

/**
 * Upload defaults the user chose once. Compliance answers default to "Ask
 * every time"; nothing here is pre-filled on the user's behalf.
 */
export function YouTubeUploadDefaults({ connected }: { connected: boolean }) {
  const locale = browserLocale();
  const [defaults, setDefaults] = useState<UploadDefaults | null>(null);
  const [categories, setCategories] = useState<Array<{ id: string; title: string }>>([]);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    getYouTubeUploadDefaults().then((result) => { if (active) setDefaults(result.defaults); }).catch(() => { if (active) setNotice("Upload defaults could not be loaded."); });
    if (connected) {
      listYouTubeCategories(regionFromLocale(locale), locale.split("-")[0] || "en")
        .then((result) => { if (active) setCategories(result.categories); })
        .catch(() => undefined);
    }
    return () => { active = false; };
  }, [connected, locale]);

  if (!defaults) return <p className="mt-4 text-xs text-[var(--muted-foreground)]">{notice ?? "Loading upload defaults…"}</p>;

  const set = (patch: Partial<UploadDefaults>) => setDefaults((current) => (current ? { ...current, ...patch } : current));
  const visibilities: Visibility[] = defaults.api_project_audited ? ["private", "schedule", "unlisted", "public"] : ["private", "schedule"];
  const timezone = defaults.timezone ?? detectTimeZone();

  async function save() {
    if (!defaults) return;
    setBusy(true);
    setNotice(null);
    try {
      const result = await saveYouTubeUploadDefaults({ ...defaults, timezone });
      setDefaults(result.defaults);
      setNotice("Upload defaults saved. They stay editable for every video.");
    } catch (reason) {
      setNotice(reason instanceof Error ? reason.message : "Upload defaults could not be saved.");
    } finally {
      setBusy(false);
    }
  }

  const tri = (label: string, value: boolean | null, onChange: (value: boolean | null) => void, yes: string, no: string) => (
    <label className="text-[11px] font-semibold">{label}
      <select value={toTri(value)} onChange={(event) => onChange(fromTri(event.target.value as TriState))} className="cf-input mt-1 text-xs">
        <option value="ask">Ask me for every video</option>
        <option value="no">{no}</option>
        <option value="yes">{yes}</option>
      </select>
    </label>
  );

  return (
    <div className="cf-subtle mt-4 rounded-[15px] border p-3 text-xs" aria-label="Upload defaults">
      <p className="text-[10px] font-bold uppercase tracking-[.1em] text-[var(--muted-foreground)]">Upload defaults</p>
      <p className="mt-1 text-[11px] text-[var(--muted-foreground)]">Pre-selected in every publishing screen, where you can still change them.</p>
      <div className="mt-3 grid gap-3 sm:grid-cols-2">
        {tri("Made for kids", defaults.made_for_kids, (made_for_kids) => set({ made_for_kids }), "Yes, made for kids", "No, not made for kids")}
        {tri("Realistic altered or synthetic content", defaults.contains_synthetic_media, (contains_synthetic_media) => set({ contains_synthetic_media }), "Yes", "No")}
        <label className="text-[11px] font-semibold">Category
          <select value={defaults.category_id ?? ""} onChange={(event) => set({ category_id: event.target.value || null })} className="cf-input mt-1 text-xs">
            <option value="">No default</option>
            {categories.map((item) => <option key={item.id} value={item.id}>{item.title}</option>)}
          </select>
        </label>
        <label className="text-[11px] font-semibold">Video language
          <input value={defaults.default_language ?? ""} onChange={(event) => set({ default_language: event.target.value.trim() || null })} placeholder="From the project" className="cf-input mt-1 text-xs" />
        </label>
        <label className="text-[11px] font-semibold">License
          <select value={defaults.license ?? "youtube"} onChange={(event) => set({ license: event.target.value as UploadDefaults["license"] })} className="cf-input mt-1 text-xs">
            <option value="youtube">Standard YouTube License</option>
            <option value="creativeCommon">Creative Commons – Attribution</option>
          </select>
        </label>
        <label className="text-[11px] font-semibold">Upload mode
          <select value={defaults.visibility ?? "private"} onChange={(event) => set({ visibility: event.target.value as Visibility })} className="cf-input mt-1 text-xs">
            {visibilities.map((item) => <option key={item} value={item}>{{ private: "Private", schedule: "Schedule", unlisted: "Unlisted", public: "Public" }[item]}</option>)}
          </select>
        </label>
        <label className="text-[11px] font-semibold">Time zone
          <input value={timezone} onChange={(event) => set({ timezone: event.target.value.trim() || null })} className="cf-input mt-1 text-xs" />
          <span className="mt-0.5 block font-normal text-[var(--muted-foreground)]">{zoneLabel(timezone)}</span>
        </label>
        <div className="space-y-2 pt-1">
          <label className="flex items-center justify-between gap-2"><span>Allow embedding</span><input type="checkbox" checked={defaults.embeddable ?? true} onChange={(event) => set({ embeddable: event.target.checked })} className="accent-[#ff6838]" /></label>
          <label className="flex items-center justify-between gap-2"><span>Notify subscribers</span><input type="checkbox" checked={defaults.notify_subscribers ?? true} onChange={(event) => set({ notify_subscribers: event.target.checked })} className="accent-[#ff6838]" /></label>
          <label className="flex items-center justify-between gap-2"><span>Show public statistics</span><input type="checkbox" checked={defaults.public_stats_viewable ?? true} onChange={(event) => set({ public_stats_viewable: event.target.checked })} className="accent-[#ff6838]" /></label>
        </div>
        <label className="flex items-start gap-2 sm:col-span-2">
          <input type="checkbox" checked={defaults.api_project_audited} onChange={(event) => set({ api_project_audited: event.target.checked, visibility: event.target.checked ? defaults.visibility : defaults.visibility === "public" || defaults.visibility === "unlisted" ? "private" : defaults.visibility })} className="mt-0.5 accent-[#ff6838]" />
          <span><span className="font-semibold">My Google API project passed YouTube&apos;s audit</span><span className="block text-[11px] text-[var(--muted-foreground)]">Until then YouTube keeps every API upload private, so ClipForge only offers Private and Schedule.</span></span>
        </label>
      </div>
      <div className="mt-3 flex items-center justify-end gap-3">
        {notice && <span role="status" className="text-[11px] text-[var(--muted-foreground)]">{notice}</span>}
        <Button size="sm" variant="outline" disabled={busy} onClick={() => void save()}>{busy ? <LoaderCircle className="size-3.5 animate-spin" /> : <Save className="size-3.5" />} Save defaults</Button>
      </div>
    </div>
  );
}
