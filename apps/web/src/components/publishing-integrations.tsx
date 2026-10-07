"use client";

import { useEffect, useState } from "react";
import { ChevronDown, KeyRound, LoaderCircle, LogOut, PlaySquare, Plus, RefreshCw, ShieldCheck, Star, Wifi } from "lucide-react";
import {
  ApiError,
  checkPublishingAccount,
  disconnectPublishingAccount,
  getPublishingAccounts,
  savePlatformClient,
  savePlatformConfig,
  setDefaultPublishingAccount,
  startPlatformAuthorization,
} from "@/lib/api";
import {
  PLATFORMS,
  accountStatusLabel,
  callbackNotice,
  capabilityChips,
  initialCardState,
  toggleCard,
  type AccountsOverview,
  type Platform,
  type PlatformSection,
  type PublishingAccount,
} from "@/lib/publishing";
import { badgeClass, noticeTone, restrictionTone, type BadgeTone } from "@/lib/alerts";
import { cn } from "@/lib/utils";
import { Alert } from "./ui/alert";
import { Button } from "./ui/button";
import { YouTubePublishingSchedule } from "./youtube-publishing-schedule";
import { YouTubeUploadDefaults } from "./youtube-upload-defaults";
import { PageLink } from "./page-link";

type Notice = { tone: "success" | "error" | "info"; text: string };

const PLATFORM_COPY: Record<Platform, { description: string; clientLabel: string; idLabel: string; secretLabel: string; idPlaceholder: string; icon: string }> = {
  youtube: {
    description: "Private uploads, native scheduling, status and real analytics per channel.",
    clientLabel: "Google OAuth client",
    idLabel: "Client ID",
    secretLabel: "Client secret",
    idPlaceholder: "…apps.googleusercontent.com",
    icon: "bg-red-600/10 text-red-700",
  },
  instagram: {
    description: "Reels to Instagram professional accounts (Business or Creator). The final video is uploaded straight from your computer.",
    clientLabel: "Meta app",
    idLabel: "App ID",
    secretLabel: "App secret",
    idPlaceholder: "Meta app ID",
    icon: "bg-fuchsia-600/10 text-fuchsia-700",
  },
  tiktok: {
    description: "Direct Post with TikTok's Content Posting API. Privacy and interaction options come from TikTok per account.",
    clientLabel: "TikTok developer app",
    idLabel: "Client key",
    secretLabel: "Client secret",
    idPlaceholder: "TikTok client key",
    icon: "bg-zinc-800/10 text-zinc-800 dark:text-zinc-200",
  },
};

/**
 * Settings → Integrations: one collapsible section per platform, every
 * connected account as its own card, "+ Add account" without any ClipForge
 * limit.  Credentials never reach the browser - only "Configured".
 */
export function PublishingIntegrations() {
  const [overview, setOverview] = useState<AccountsOverview | null>(null);
  const [notice, setNotice] = useState<(Notice & { platform?: Platform }) | null>(null);
  // Every fresh mount starts with all cards collapsed (see initialCardState).
  const [open, setOpen] = useState<Record<Platform, boolean>>(initialCardState);

  // The OAuth result is read once per mount (not inside the effect: React's
  // development double-run would find the URL already cleaned and drop it).
  const [callback] = useState(() => (typeof window === "undefined" ? null : callbackNotice(new URLSearchParams(window.location.search))));

  useEffect(() => {
    let active = true;
    // Intentional replace: the one-time callback result must not stay in the
    // URL or become its own history entry (Back/reload would show it again).
    if (callback) window.history.replaceState(null, "", window.location.pathname + window.location.hash);
    getPublishingAccounts()
      .then((next) => {
        if (!active) return;
        setOverview(next);
        if (callback) setNotice(callback);
      })
      .catch(() => { if (active) setNotice({ tone: "error", text: "Publishing accounts could not be loaded." }); });
    return () => { active = false; };
  }, [callback]);

  if (!overview) {
    return (
      <section className="cf-surface mt-6 rounded-[22px] border p-5 text-sm text-[var(--muted-foreground)] shadow-sm sm:p-6" aria-busy={!notice}>
        {notice?.text ?? "Loading publishing accounts…"}
      </section>
    );
  }

  return (
    <div className="mt-6 space-y-4" id="publishing">
      {notice && <NoticeLine notice={notice} />}
      {PLATFORMS.map((platform) => (
        <PlatformCard
          key={platform}
          section={overview.platforms[platform]}
          open={open[platform]}
          onToggle={() => setOpen((current) => toggleCard(current, platform))}
          onOverview={setOverview}
          schedulerNotice={platform === "youtube" ? null : overview.scheduler.notice}
        />
      ))}
    </div>
  );
}

function accountStatusTone(status: PublishingAccount["status"]): BadgeTone {
  return status === "connected" ? "success" : status === "disconnected" ? "muted" : "error";
}

function NoticeLine({ notice }: { notice: Notice }) {
  return (
    <Alert tone={noticeTone(notice.tone)}>{notice.text}</Alert>
  );
}

function PlatformCard({ section, open, onToggle, onOverview, schedulerNotice }: {
  section: PlatformSection;
  open: boolean;
  onToggle: () => void;
  onOverview: (next: AccountsOverview) => void;
  schedulerNotice: string | null;
}) {
  const platform = section.platform;
  const copy = PLATFORM_COPY[platform];
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const clientReady = section.client.configured;
  const connected = section.accounts.filter((item) => item.status === "connected").length;

  async function run(key: string, work: () => Promise<AccountsOverview | void>, success?: string) {
    setBusy(key);
    setNotice(null);
    try {
      const next = await work();
      if (next) onOverview(next);
      if (success) setNotice({ tone: "success", text: success });
    } catch (reason) {
      setNotice({ tone: "error", text: reason instanceof ApiError ? reason.message : "The request failed." });
    } finally {
      setBusy(null);
    }
  }

  async function authorize(accountId?: string) {
    setBusy(accountId ? `reconnect:${accountId}` : "add");
    setNotice(null);
    try {
      const { authorization_url } = await startPlatformAuthorization(platform, accountId);
      window.location.assign(authorization_url);
    } catch (reason) {
      setNotice({ tone: "error", text: reason instanceof ApiError ? reason.message : "Sign-in could not start." });
      setBusy(null);
    }
  }

  return (
    <section id={platform} className="cf-surface rounded-[22px] border shadow-sm backdrop-blur" aria-label={section.label}>
      <button type="button" onClick={onToggle} aria-expanded={open} aria-controls={`${platform}-accounts`} className="flex w-full items-start justify-between gap-3 p-5 text-left sm:p-6">
        <span className="flex gap-3.5">
          <span className={cn("grid size-10 shrink-0 place-items-center rounded-[13px]", copy.icon)}><PlaySquare className="size-[18px]" /></span>
          <span className="min-w-0">
            <span className="block text-lg font-bold tracking-[-.025em]">{section.label}</span>
            <span className="mt-1 block max-w-xl text-xs leading-5 text-[var(--muted-foreground)]">{copy.description}</span>
            <span className="mt-1 block text-[11px] font-semibold text-[var(--muted-foreground)]">
              {section.accounts.length === 0 ? "No accounts" : `${section.accounts.length} account${section.accounts.length === 1 ? "" : "s"}${connected !== section.accounts.length ? ` · ${connected} connected` : ""}`}
              {" · "}<span className={clientReady ? "cf-text-success" : "cf-text-warning"}>{clientReady ? "App configured" : "App not configured"}</span>
            </span>
          </span>
        </span>
        <ChevronDown className={cn("mt-1 size-4 shrink-0 transition", open && "rotate-180")} aria-hidden />
      </button>

      {open && (
        <div id={`${platform}-accounts`} className="border-t border-[var(--border)] p-5 sm:p-6">
          <ClientConfig section={section} busy={busy} onRun={run} />

          <div className="mt-4 space-y-3">
            {section.accounts.map((account) => (
              <AccountCard key={account.id} account={account} busy={busy} clientReady={clientReady}
                onReconnect={() => void authorize(account.id)}
                onDisconnect={() => run(`disconnect:${account.id}`, () => disconnectPublishingAccount(account.id), `${account.display_name} disconnected. Its publication history is kept.`)}
                onDefault={() => run(`default:${account.id}`, () => setDefaultPublishingAccount(account.id), `${account.display_name} is now the default ${section.label} account.`)}
                onCheck={() => run(`check:${account.id}`, async () => {
                  const result = await checkPublishingAccount(account.id);
                  if (!result.ok) throw new ApiError(result.error?.message ?? "The account check failed.", 400);
                  return getPublishingAccounts();
                }, "Connection verified (read-only check).")}
              />
            ))}
          </div>

          <div className="mt-4 flex flex-wrap items-center gap-2">
            <Button size="sm" variant="accent" disabled={!clientReady || !!busy} onClick={() => void authorize()} title={clientReady ? undefined : "Configure the developer app first"}>
              {busy === "add" ? <LoaderCircle className="size-3.5 animate-spin" /> : <Plus className="size-3.5" />} Add account
            </Button>
            <span className="text-[11px] text-[var(--muted-foreground)]">
              {platform === "youtube" && "Pick another Google account or brand channel in Google's account chooser to add more channels."}
              {platform === "instagram" && "Sign in with the Facebook profile that manages the Instagram account's Page; pick the accounts to add. One sign-in can add several."}
              {platform === "tiktok" && "Sign in as another TikTok creator to add more accounts."}
            </span>
          </div>

          {schedulerNotice && <Alert tone="info" size="sm" role="none" className="mt-4">{schedulerNotice}</Alert>}
          {notice && <div className="mt-4"><NoticeLine notice={notice} /></div>}

          {platform === "youtube" && connected > 0 && (
            <>
              <p className="mt-5 text-[11px] text-[var(--muted-foreground)]">Publishing schedule and upload defaults below apply to the default YouTube channel.</p>
              <YouTubePublishingSchedule />
            </>
          )}
          {platform === "youtube" && <YouTubeUploadDefaults connected={connected > 0} />}
          {platform === "youtube" && (
            <p className="mt-4 text-[11px] text-[var(--muted-foreground)]">
              Published videos stay in <PageLink href="/videos" className="underline">Videos</PageLink> — also after their project is deleted.
            </p>
          )}
        </div>
      )}
    </section>
  );
}

function AccountCard({ account, busy, clientReady, onReconnect, onDisconnect, onDefault, onCheck }: {
  account: PublishingAccount;
  busy: string | null;
  clientReady: boolean;
  onReconnect: () => void;
  onDisconnect: () => void;
  onDefault: () => void;
  onCheck: () => void;
}) {
  const [confirm, setConfirm] = useState(false);
  const chips = capabilityChips(account.capabilities);
  const needsReconnect = account.status !== "connected";
  return (
    <article className="rounded-[15px] border border-[var(--border)] p-3" aria-label={`${account.platform_label} account ${account.display_name}`}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex min-w-0 gap-3">
          {account.avatar_url ? (
            // eslint-disable-next-line @next/next/no-img-element -- provider avatar URL, small and already sized
            <img src={account.avatar_url} alt="" className="size-10 shrink-0 rounded-full object-cover" referrerPolicy="no-referrer" />
          ) : (
            <span className="grid size-10 shrink-0 place-items-center rounded-full bg-black/[.06] text-sm font-bold">{account.display_name.slice(0, 1).toUpperCase()}</span>
          )}
          <div className="min-w-0">
            <p className="flex flex-wrap items-center gap-1.5 text-sm font-semibold">
              {account.display_name}
              {account.is_default && <span className="inline-flex items-center gap-0.5 rounded-full bg-[var(--accent-soft)] px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-[.08em] text-[#d94c20]"><Star className="size-2.5" /> Default</span>}
              <span className={badgeClass(accountStatusTone(account.status))}>{accountStatusLabel(account)}</span>
            </p>
            {account.handle && account.platform !== "youtube" && <p className="text-xs text-[var(--muted-foreground)]">@{account.handle.replace(/^@/, "")}</p>}
            {account.platform === "youtube" && <p className="mono text-[10px] text-[var(--muted-foreground)]">{account.external_account_id}</p>}
            {chips.length > 0 && <p className="mt-1.5 flex flex-wrap gap-1">{chips.map((chip) => <span key={chip} className="rounded-full bg-black/[.04] px-1.5 py-0.5 text-[9px] font-semibold">{chip}</span>)}</p>}
          </div>
        </div>
        <div className="flex flex-wrap gap-1.5">
          {!account.is_default && account.status === "connected" && <Button size="sm" variant="ghost" disabled={!!busy} onClick={onDefault}><Star className="size-3.5" /> Make default</Button>}
          {account.status === "connected" && <Button size="sm" variant="ghost" disabled={!!busy} onClick={onCheck}>{busy === `check:${account.id}` ? <LoaderCircle className="size-3.5 animate-spin" /> : <Wifi className="size-3.5" />} Check</Button>}
          <Button size="sm" variant={needsReconnect ? "accent" : "outline"} disabled={!clientReady || !!busy} onClick={onReconnect}>{busy === `reconnect:${account.id}` ? <LoaderCircle className="size-3.5 animate-spin" /> : <RefreshCw className="size-3.5" />} Reconnect</Button>
          <Button size="sm" variant="ghost" disabled={!!busy} onClick={() => setConfirm(true)}><LogOut className="size-3.5" /> Disconnect</Button>
        </div>
      </div>
      {account.error?.message && <Alert tone="error" size="sm" className="mt-2">{account.error.message}</Alert>}
      {account.restrictions.map((item) => (
        <Alert key={item.code} tone={restrictionTone(item)} size="sm" role="none" className="mt-2">{item.message}</Alert>
      ))}
      {confirm && (
        <div className="cf-tone-error mt-3 rounded-[12px] border p-3">
          <p className="text-xs font-bold">Disconnect {account.display_name}?</p>
          <p className="mt-1 text-[11px] leading-4">Only this account&apos;s stored sign-in is removed. Other accounts stay connected; its posts and history stay unchanged.</p>
          <div className="mt-2 flex justify-end gap-2">
            <Button size="sm" variant="ghost" onClick={() => setConfirm(false)}>Cancel</Button>
            <Button size="sm" className="bg-red-700 hover:bg-red-800" disabled={!!busy} onClick={() => { setConfirm(false); onDisconnect(); }}>{busy === `disconnect:${account.id}` ? <LoaderCircle className="size-3.5 animate-spin" /> : <LogOut className="size-3.5" />} Disconnect</Button>
          </div>
        </div>
      )}
    </article>
  );
}

function ClientConfig({ section, busy, onRun }: { section: PlatformSection; busy: string | null; onRun: (key: string, work: () => Promise<AccountsOverview | void>, success?: string) => Promise<void> }) {
  const platform = section.platform;
  const copy = PLATFORM_COPY[platform];
  const [editing, setEditing] = useState(false);
  const [clientId, setClientId] = useState("");
  const [secret, setSecret] = useState("");
  const [configId, setConfigId] = useState(String(section.client.login_config_id ?? ""));
  const ready = section.client.configured;
  return (
    <div className="cf-subtle rounded-[15px] border p-3 text-xs">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="flex items-center gap-1.5 text-[10px] font-bold uppercase tracking-[.1em] text-[var(--muted-foreground)]"><KeyRound className="size-3" /> {copy.clientLabel}</span>
        <span className={cn("text-[10px] font-bold", ready ? "cf-text-success" : "cf-text-warning")}>{ready ? `Configured (${section.client.client_id_source === "keyring" ? "secure storage" : "environment"})` : "Not configured"}</span>
      </div>
      <p className="mt-2 text-[11px] leading-5 text-[var(--muted-foreground)]">Register this redirect URI in the developer app: <span className="mono break-all text-[var(--foreground)]">{section.client.redirect_uri}</span></p>
      {section.client.scopes && <p className="text-[11px] leading-5 text-[var(--muted-foreground)]">Requested permissions: <span className="mono">{section.client.scopes.join(", ")}</span></p>}
      {platform === "tiktok" && (
        <label className="mt-2 flex items-start gap-2 text-[11px]">
          <input type="checkbox" checked={!!section.client.app_audited} disabled={!!busy} onChange={(event) => void onRun("audited", () => savePlatformConfig("tiktok", { app_audited: event.target.checked }), event.target.checked ? "Public Direct Post enabled for an audited app." : "Posts are limited to private (Only me).")} className="mt-0.5 accent-[#ff6838]" />
          <span><span className="font-semibold">This TikTok app passed TikTok&apos;s Content Posting audit.</span> Public Direct Post requires TikTok app approval; until then ClipForge offers only private (Only me) posts.</span>
        </label>
      )}
      {platform === "instagram" && (
        <>
          {/* Why the sign-in goes through Facebook: Meta's direct "Instagram Login" API accepts
              Reels only from a public video URL; uploading the local final MP4 (resumable
              upload) is offered only with Facebook Login for Business. */}
          <p className="mt-2 text-[11px] leading-5 text-[var(--muted-foreground)]">
            Sign-in uses Facebook because Meta only accepts a video file from your computer through Facebook Login.
            Meta&apos;s direct Instagram Login requires every video to be hosted at a public URL, which ClipForge does not do.
          </p>
          <details className="mt-2">
            <summary className="cursor-pointer text-[11px] font-semibold text-[var(--muted-foreground)]">Advanced</summary>
            <form className="mt-2 flex flex-wrap items-end gap-2" onSubmit={(event) => { event.preventDefault(); void onRun("config", () => savePlatformConfig("instagram", { login_config_id: configId.trim() || null }), "Login configuration saved."); }}>
              <label className="text-[11px] font-semibold">Login configuration ID (optional)
                <input value={configId} onChange={(event) => setConfigId(event.target.value.replace(/\D/g, ""))} className="cf-input mt-1 w-56 text-xs" inputMode="numeric" placeholder="Uses the scope list when empty" />
              </label>
              <Button type="submit" size="sm" variant="outline" disabled={!!busy}>Save</Button>
            </form>
          </details>
        </>
      )}
      {!editing && <button type="button" className="interactive-text mt-1" onClick={() => setEditing(true)} disabled={!!busy}><KeyRound className="size-3.5" /> {ready ? "Change app credentials" : "Add app credentials"}</button>}
      {editing && (
        <form className="mt-3 grid gap-2 sm:grid-cols-2" onSubmit={(event) => { event.preventDefault(); void onRun("client", async () => { const next = await savePlatformClient(platform, clientId.trim(), secret.trim() || null); setEditing(false); setClientId(""); setSecret(""); return next; }, "App credentials saved securely."); }}>
          <label className="text-[11px] font-semibold">{copy.idLabel}<input value={clientId} onChange={(event) => setClientId(event.target.value)} className="cf-input mt-1 text-xs" autoComplete="off" spellCheck={false} placeholder={copy.idPlaceholder} /></label>
          <label className="text-[11px] font-semibold">{copy.secretLabel}<input type="password" value={secret} onChange={(event) => setSecret(event.target.value)} className="cf-input mt-1 text-xs" autoComplete="off" spellCheck={false} placeholder="Stored in the system keychain" /></label>
          <div className="flex justify-end gap-2 sm:col-span-2">
            <Button type="button" size="sm" variant="ghost" onClick={() => setEditing(false)}>Cancel</Button>
            <Button type="submit" size="sm" variant="accent" disabled={clientId.trim().length < 4 || busy === "client"}>{busy === "client" ? <LoaderCircle className="size-3.5 animate-spin" /> : <ShieldCheck className="size-3.5" />} Save securely</Button>
          </div>
        </form>
      )}
    </div>
  );
}
