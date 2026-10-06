"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { ExternalLink, LoaderCircle, Upload } from "lucide-react";
import { ApiError, createSocialPublication, getSocialDraft, getSocialPublication, preflightSocialPublication, type SocialPublicationBody } from "@/lib/api";
import type { Project } from "@/lib/types";
import {
  PLATFORM_LABELS,
  captionLength,
  composeCaption,
  hashtagCount,
  parseHashtags,
  publicationTone,
  socialFields,
  socialIssues,
  socialSubmitLabel,
  tiktokInteraction,
  tiktokPrivacyChoices,
  type InstagramOptions,
  type PublishingAccount,
  type SocialDraft,
  type SocialPublication,
  type TikTokOptions,
} from "@/lib/publishing";
import { browserLocale, detectTimeZone, formatScheduleConfirmation, type ScheduleChoice, type ScheduleResolution } from "@/lib/youtube";
import { cn } from "@/lib/utils";
import { restrictionTone } from "@/lib/alerts";
import { PublishShell } from "./publish-shell";
import { Alert } from "./ui/alert";
import { Button } from "./ui/button";
import { ScheduleFields } from "./youtube-schedule-fields";

const FOLLOW_MS = 4000;
const MAX_FOLLOW = 90;

function Section({ title, hint, children }: { title: string; hint?: string; children: React.ReactNode }) {
  return (
    <section className="border-t border-[var(--border)] px-5 py-5 first:border-t-0 sm:px-6">
      <h3 className="text-[11px] font-bold uppercase tracking-[.12em] text-[var(--muted-foreground)]">{title}</h3>
      {hint && <p className="mt-1 text-xs text-[var(--muted-foreground)]">{hint}</p>}
      <div className="mt-3">{children}</div>
    </section>
  );
}

function Toggle({ label, checked, onChange, hint, disabled }: { label: string; checked: boolean; onChange: (value: boolean) => void; hint?: string | null; disabled?: boolean }) {
  return (
    <label className={cn("flex items-start justify-between gap-3 text-sm", disabled && "opacity-60")}>
      <span><span className="font-medium">{label}</span>{hint && <span className="block text-[11px] text-[var(--muted-foreground)]">{hint}</span>}</span>
      <input type="checkbox" role="switch" checked={checked} disabled={disabled} onChange={(event) => onChange(event.target.checked)} className="mt-1 size-4 accent-[#ff6838]" />
    </label>
  );
}

/**
 * The Instagram / TikTok part of the unified Upload sheet.  Every control is
 * derived from the selected account's capabilities (and, for TikTok, the
 * creator_info TikTok returned just now); nothing pretends to be YouTube.
 */
export function SocialPublishSheet({ project, account, onClose, onPublished, selector }: {
  project: Project;
  account: PublishingAccount;
  onClose: () => void;
  onPublished: () => void;
  selector?: React.ReactNode;
}) {
  const locale = browserLocale();
  const [draft, setDraft] = useState<SocialDraft | null>(null);
  const [options, setOptions] = useState<Record<string, unknown> | null>(null);
  const [hashtagsText, setHashtagsText] = useState("");
  const [mode, setMode] = useState<"now" | "schedule">("now");
  const [schedule, setSchedule] = useState<ScheduleChoice>(() => ({ date: "", time: "", timezone: detectTimeZone() }));
  const [resolution, setResolution] = useState<ScheduleResolution | null>(null);
  const [serverIssues, setServerIssues] = useState<Array<{ field: string; message: string }>>([]);
  const [error, setError] = useState<string | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<SocialPublication | null>(null);
  const [follows, setFollows] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    getSocialDraft(project.id, account.id, controller.signal)
      .then((next) => {
        setDraft(next);
        setOptions(next.options);
        setHashtagsText(((next.options.hashtags as string[]) ?? []).join(" "));
      })
      .catch((reason) => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "The publishing settings could not be loaded."); });
    return () => controller.abort();
  }, [project.id, account.id]);

  const platform = account.platform as "instagram" | "tiktok";
  const body = useMemo<SocialPublicationBody | null>(() => {
    if (!options) return null;
    const values = { ...options, hashtags: parseHashtags(hashtagsText) };
    return {
      account_id: account.id,
      base_revision: project.current_revision,
      mode,
      schedule: mode === "schedule" ? schedule : null,
      ...(platform === "instagram" ? { instagram: values as InstagramOptions } : { tiktok: values as TikTokOptions }),
    };
  }, [options, hashtagsText, account.id, project.current_revision, mode, schedule, platform]);

  // The backend preflight is the authority (it re-reads creator_info for TikTok).
  useEffect(() => {
    if (!body) return;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      preflightSocialPublication(project.id, body, controller.signal)
        .then((next) => setServerIssues(next.issues))
        .catch(() => undefined);
    }, 400);
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [body, project.id]);

  // Follow a "publish now" post until the provider answers (bounded).
  useEffect(() => {
    if (!result || !["pending", "uploading", "processing"].includes(result.state) || follows >= MAX_FOLLOW) return;
    const timer = window.setTimeout(() => {
      getSocialPublication(result.id)
        .then((next) => { setResult(next.publication); setFollows((value) => value + 1); })
        .catch(() => setFollows((value) => value + 1));
    }, FOLLOW_MS);
    return () => window.clearTimeout(timer);
  }, [result, follows]);

  if (!draft || !options) {
    return (
      <PublishShell onClose={onClose} selector={selector}>
        <div className="grid min-h-60 place-items-center p-6 text-sm text-[var(--muted-foreground)]">
          {error ? <Alert tone="error">{error}</Alert> : <span className="flex items-center gap-2"><LoaderCircle className="size-4 animate-spin" /> Preparing your post…</span>}
        </div>
      </PublishShell>
    );
  }

  const caps = draft.capabilities;
  const fields = socialFields(caps);
  const update = (patch: Record<string, unknown>) => setOptions((current) => (current ? { ...current, ...patch } : current));
  const finalCaption = composeCaption(String(options.caption ?? ""), parseHashtags(hashtagsText));
  const scheduleReady = mode !== "schedule" || resolution?.status === "ok";
  const localIssues = socialIssues(draft, { ...options, hashtags: parseHashtags(hashtagsText) }, mode, scheduleReady);
  const issues = Array.from(new Set([...localIssues, ...serverIssues.map((item) => item.message)]));
  const locked = busy || !!result;
  const creator = draft.creator_info;
  const privacy = tiktokPrivacyChoices(creator, !!draft.app_audited);
  const duration = draft.duration_seconds ?? 0;

  async function submit() {
    if (!body || issues.length || locked) return;
    setBusy(true);
    setSubmitError(null);
    try {
      const response = await createSocialPublication(project.id, body);
      setResult(response.publication);
      if (response.publication.state === "scheduled") window.setTimeout(onPublished, 2500);
    } catch (reason) {
      setSubmitError(reason instanceof ApiError ? reason.message : "The post could not be created.");
    } finally {
      setBusy(false);
    }
  }

  const tone = result ? publicationTone(result.state) : null;

  return (
    <PublishShell onClose={onClose} selector={selector} subtitle={<>{PLATFORM_LABELS[platform]} · the same final video YouTube gets (narration, music, current audio). <Link href="/settings/integrations" className="underline">Accounts</Link></>}>
      <div className="min-h-0 flex-1 overflow-y-auto">
        {(draft.account.restrictions.length > 0 || caps.notes.length > 0) && (
          <div className="mx-5 mt-4 space-y-1.5 sm:mx-6">
            {draft.account.restrictions.map((item) => (
              <Alert key={item.code} tone={restrictionTone(item)} size="sm">{item.message}</Alert>
            ))}
            {caps.notes.filter((note) => !draft.account.restrictions.some((item) => item.message === note)).map((note) => (
              <Alert key={note} tone="info" size="sm" role="none">{note}</Alert>
            ))}
          </div>
        )}
        {draft.creator_info_error && <Alert tone="error" className="mx-5 mt-3 sm:mx-6">{draft.creator_info_error.message}</Alert>}

        <fieldset disabled={locked} className="m-0 min-w-0 border-0 p-0">
          {fields.includes("caption") && (
            <Section title="Caption" hint={platform === "tiktok" ? "TikTok shows this as the video's caption (its API calls it the title). Hashtags are added to it." : "Instagram has no separate title: this caption is the post text. Hashtags are added to it."}>
              <textarea value={String(options.caption ?? "")} onChange={(event) => update({ caption: event.target.value })} rows={4} className="cf-input text-sm" aria-label="Caption" />
              {fields.includes("hashtags") && (
                <label className="mt-3 block text-xs font-semibold">Hashtags
                  <input value={hashtagsText} onChange={(event) => setHashtagsText(event.target.value)} className="cf-input mt-1 text-sm" placeholder="#topic #learn" />
                </label>
              )}
              <div className="mt-2 flex flex-wrap justify-between gap-2 text-[10px] text-[var(--muted-foreground)]">
                <span>From ClipForge&apos;s {PLATFORM_LABELS[platform]} metadata{draft.metadata_source !== "social_metadata" ? " (not generated yet: project title)" : ""}</span>
                <span className={cn("mono", captionLength(finalCaption) > draft.caption_limit && "cf-text-error font-bold")}>{captionLength(finalCaption)}/{draft.caption_limit}{draft.hashtag_limit ? ` · ${hashtagCount(finalCaption)}/${draft.hashtag_limit} hashtags` : ""}</span>
              </div>
              <pre className="mt-2 max-h-32 overflow-y-auto whitespace-pre-wrap rounded-lg bg-black/[.03] p-2 text-[11px]" aria-label="Final caption">{finalCaption || "—"}</pre>
            </Section>
          )}

          {fields.includes("privacy") && (
            <Section title="Who can view this video" hint="Options come from TikTok for this account right now. Choose one - there is no default.">
              <div className="grid gap-2 sm:grid-cols-2">
                {privacy.map((item) => (
                  <label key={item.value} className={cn("flex cursor-pointer items-start gap-2 rounded-xl border px-3 py-2 text-sm", options.privacy_level === item.value ? "border-[#ff6838] bg-[var(--accent-soft)]" : "border-[var(--border)]", item.disabled && "cursor-not-allowed opacity-60")}>
                    <input type="radio" name="privacy" checked={options.privacy_level === item.value} disabled={item.disabled} onChange={() => update({ privacy_level: item.value })} className="mt-1 accent-[#ff6838]" />
                    <span><span className="font-semibold">{item.label}</span>{item.reason && <span className="block text-[11px] text-[var(--muted-foreground)]">{item.reason}</span>}</span>
                  </label>
                ))}
                {!privacy.length && <p className="text-xs text-[var(--muted-foreground)]">TikTok&apos;s privacy options are not available.</p>}
              </div>
            </Section>
          )}

          {(fields.includes("comments") || fields.includes("duet") || fields.includes("stitch")) && (
            <Section title="Interactions">
              <div className="space-y-3">
                {(["comments", "duet", "stitch"] as const).filter((kind) => fields.includes(kind)).map((kind) => {
                  const state = tiktokInteraction(creator, kind);
                  const key = kind === "comments" ? "allow_comments" : kind === "duet" ? "allow_duet" : "allow_stitch";
                  return <Toggle key={kind} label={`Allow ${kind === "comments" ? "comments" : kind}`} checked={!!options[key]} disabled={state.disabled} hint={state.reason} onChange={(value) => update({ [key]: value })} />;
                })}
              </div>
            </Section>
          )}

          {(fields.includes("cover") || fields.includes("share_to_feed")) && (
            <Section title={platform === "instagram" ? "Reel" : "Cover"} hint={caps.cover_mode === "frame" ? "The cover is a frame of this video. Custom cover images are not supported by this publishing flow without a public file URL." : undefined}>
              <div className="space-y-3">
                {fields.includes("cover") && (
                  <label className="block text-xs font-semibold">Cover frame at (seconds)
                    <input type="number" min={0} max={duration || undefined} step={0.1} value={options.cover_frame_ms === null || options.cover_frame_ms === undefined ? "" : Number(options.cover_frame_ms) / 1000}
                      onChange={(event) => update({ cover_frame_ms: event.target.value === "" ? null : Math.round(Number(event.target.value) * 1000) })}
                      className="cf-input mt-1 w-40 text-sm" placeholder={platform === "instagram" ? "Instagram picks" : "TikTok picks"} />
                  </label>
                )}
                {fields.includes("share_to_feed") && <Toggle label="Also show in the profile feed" checked={!!options.share_to_feed} onChange={(value) => update({ share_to_feed: value })} />}
              </div>
            </Section>
          )}

          {fields.includes("ai_disclosure") && (
            <Section title="AI-generated content" hint={draft.ai_suggestion.why}>
              <div className="flex gap-2">
                {[true, false].map((value) => (
                  <label key={String(value)} className={cn("flex cursor-pointer items-center gap-2 rounded-xl border px-3 py-2 text-sm", options.is_aigc === value ? "border-[#ff6838] bg-[var(--accent-soft)]" : "border-[var(--border)]")}>
                    <input type="radio" name="aigc" checked={options.is_aigc === value} onChange={() => update({ is_aigc: value })} className="accent-[#ff6838]" />
                    {value ? "Yes, label as AI-generated" : "No"}
                  </label>
                ))}
              </div>
            </Section>
          )}

          {fields.includes("commercial") && (
            <Section title="Commercial content" hint="Disclose if this video promotes a brand, product or service.">
              <div className="space-y-3">
                <Toggle label="Your brand" hint="You are promoting yourself or your own business." checked={!!options.brand_organic} onChange={(value) => update({ brand_organic: value })} />
                <Toggle label="Branded content" hint="Paid partnership with a third party (cannot be private)." checked={!!options.brand_content} onChange={(value) => update({ brand_content: value })} />
              </div>
            </Section>
          )}

          {fields.includes("schedule") && (
            <Section title="When" hint={draft.scheduling.notice}>
              <div className="flex flex-wrap gap-2">
                {(["now", "schedule"] as const).map((value) => (
                  <label key={value} className={cn("flex cursor-pointer items-center gap-2 rounded-xl border px-3 py-2 text-sm", mode === value ? "border-[#ff6838] bg-[var(--accent-soft)]" : "border-[var(--border)]")}>
                    <input type="radio" name="when" checked={mode === value} onChange={() => setMode(value)} className="accent-[#ff6838]" />
                    {value === "now" ? "Publish now" : "Schedule (ClipForge publishes it)"}
                  </label>
                ))}
              </div>
              {mode === "schedule" && (
                <div className="mt-3">
                  <ScheduleFields value={schedule} onChange={setSchedule} locale={locale} onResolved={setResolution} />
                  <Alert tone="warning" size="sm" role="none" className="mt-2">Runs only while the ClipForge backend is running with internet access. A post whose time passes while your Mac is off is marked Missed and waits for you.</Alert>
                </div>
              )}
            </Section>
          )}

          {draft.music_usage_confirmation && (
            <Section title="Confirmation">
              <label className="flex items-start gap-2 text-sm">
                <input type="checkbox" checked={!!options.music_usage_confirmed} onChange={(event) => update({ music_usage_confirmed: event.target.checked })} className="mt-1 accent-[#ff6838]" />
                <span>{draft.music_usage_confirmation}</span>
              </label>
            </Section>
          )}
        </fieldset>

        {draft.existing.length > 0 && (
          <Section title="Earlier posts of this project on this account">
            <ul className="space-y-1 text-xs">
              {draft.existing.map((item) => <li key={item.id}>{item.state_label}{item.scheduled_at ? ` · ${new Date(item.scheduled_at).toLocaleString(locale)}` : ""}{item.error?.message ? ` · ${item.error.message}` : ""}</li>)}
            </ul>
          </Section>
        )}
      </div>

      <footer className="border-t border-[var(--border)] px-5 py-4 sm:px-6">
        {issues.length > 0 && !result && (
          <Alert id="social-blocked-reason" tone="warning" size="sm" role="none" className="mb-3" title="Before you can upload:">
            <ul className="mt-0.5 list-disc space-y-0.5 pl-4">
              {issues.map((item) => <li key={item}>{item}</li>)}
            </ul>
          </Alert>
        )}
        {result && (
          <Alert tone={tone === "success" ? "success" : tone === "error" ? "error" : tone === "warn" ? "warning" : "info"} role="status" icon={tone !== "busy"} className="mb-3">
            {tone === "busy" && <LoaderCircle className="mr-1.5 inline size-3 animate-spin" aria-hidden />}
            <span>
              {result.state_label}
              {result.state === "scheduled" && result.scheduled_at && mode === "schedule" ? ` for ${formatScheduleConfirmation(result.scheduled_at, schedule.timezone, locale)}` : ""}
              {result.error?.message ? ` · ${result.error.message}` : ""}
              {result.remote_url && <> · <a href={result.remote_url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 underline">Open <ExternalLink className="size-3" /></a></>}
            </span>
          </Alert>
        )}
        {submitError && <Alert tone="error" className="mb-3">{submitError}</Alert>}
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={result ? onPublished : onClose}>{result ? "Done" : "Cancel"}</Button>
          {!result && (
            <Button variant="accent" onClick={() => void submit()} disabled={issues.length > 0 || busy} aria-describedby="social-blocked-reason">
              {busy ? <LoaderCircle className="size-3.5 animate-spin" /> : <Upload className="size-3.5" />} {socialSubmitLabel(mode)}
            </Button>
          )}
        </div>
      </footer>
    </PublishShell>
  );
}
