"use client";

import { useEffect, useState } from "react";
import { AlertCircle, CheckCircle2, KeyRound, LoaderCircle, LogOut, PlaySquare, RefreshCw, ShieldCheck } from "lucide-react";
import { ApiError, disconnectYouTube, getYouTubeConnection, saveYouTubeClient, startYouTubeAuthorization } from "@/lib/api";
import type { YouTubeConnection } from "@/lib/youtube";
import { cn } from "@/lib/utils";
import Link from "next/link";
import { Button } from "./ui/button";
import { YouTubePublishingSchedule } from "./youtube-publishing-schedule";
import { YouTubeUploadDefaults } from "./youtube-upload-defaults";

type Notice = { tone: "success" | "error" | "info"; text: string };

const CALLBACK_REASONS: Record<string, string> = {
  access_denied: "Google sign-in was cancelled. Nothing was connected.",
  invalid_state: "The sign-in link expired or was already used. Start again.",
  no_channel: "This Google account has no YouTube channel.",
  no_refresh_token: "Google did not grant offline access. Remove ClipForge from your Google account permissions and connect again.",
  storage_error: "Secure storage is unavailable, so YouTube was not connected.",
  api_disabled: "Enable the YouTube Data API v3 and YouTube Analytics API for your Google Cloud project.",
};

const CAPABILITY_LABELS: Record<string, string> = { upload: "Upload", read: "Read status", schedule: "Schedule", analytics: "Analytics" };

export function YouTubeConnectionCard() {
  const [connection, setConnection] = useState<YouTubeConnection | null>(null);
  const [busy, setBusy] = useState<"connect" | "disconnect" | "client" | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [confirmDisconnect, setConfirmDisconnect] = useState(false);
  const [editingClient, setEditingClient] = useState(false);
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");

  useEffect(() => {
    let active = true;
    // The OAuth callback redirects here with ?youtube=connected|error&reason=<code>.
    const params = new URLSearchParams(window.location.search);
    const result = params.get("youtube");
    if (result) window.history.replaceState(null, "", window.location.pathname + window.location.hash);
    getYouTubeConnection()
      .then((next) => {
        if (!active) return;
        setConnection(next);
        if (result === "connected") setNotice({ tone: "success", text: "YouTube connected." });
        if (result === "error") setNotice({ tone: "error", text: CALLBACK_REASONS[params.get("reason") ?? ""] ?? "YouTube could not be connected. Try again." });
      })
      .catch(() => { if (active) setNotice({ tone: "error", text: "YouTube status could not be loaded." }); });
    return () => { active = false; };
  }, []);

  async function connect() {
    setBusy("connect");
    setNotice(null);
    try {
      const { authorization_url } = await startYouTubeAuthorization();
      window.location.assign(authorization_url);
    } catch (reason) {
      setNotice({ tone: "error", text: reason instanceof ApiError ? reason.message : "YouTube sign-in could not start." });
      setBusy(null);
    }
  }

  async function disconnect() {
    setBusy("disconnect");
    setNotice(null);
    try {
      setConnection(await disconnectYouTube());
      setConfirmDisconnect(false);
      setNotice({ tone: "success", text: "YouTube disconnected. Existing upload history is kept." });
    } catch (reason) {
      setNotice({ tone: "error", text: reason instanceof ApiError ? reason.message : "YouTube could not be disconnected." });
    } finally {
      setBusy(null);
    }
  }

  async function saveClient() {
    setBusy("client");
    setNotice(null);
    try {
      setConnection(await saveYouTubeClient(clientId.trim(), clientSecret.trim() || null));
      setEditingClient(false);
      setClientId("");
      setClientSecret("");
      setNotice({ tone: "success", text: "Google OAuth client saved securely." });
    } catch (reason) {
      setNotice({ tone: "error", text: reason instanceof ApiError ? reason.message : "The OAuth client could not be saved." });
    } finally {
      setBusy(null);
    }
  }

  const connected = connection?.status === "connected";
  const expired = connection?.status === "auth_expired";
  const clientReady = connection?.client.configured ?? false;
  const missing = connected ? Object.entries(CAPABILITY_LABELS).filter(([key]) => !connection?.capabilities[key as keyof YouTubeConnection["capabilities"]]) : [];

  return (
    <section id="youtube" className="cf-surface mt-6 rounded-[22px] border p-5 shadow-sm backdrop-blur sm:p-6" aria-label="YouTube">
      <div className="flex flex-col justify-between gap-4 sm:flex-row sm:items-start">
        <div className="flex gap-3.5">
          <div className="grid size-10 shrink-0 place-items-center rounded-[13px] bg-red-600/10 text-red-700"><PlaySquare className="size-[18px]" /></div>
          <div className="min-w-0">
            <h2 className="text-lg font-bold tracking-[-.025em]">YouTube</h2>
            <p className="mt-1 max-w-xl text-xs leading-5 text-[var(--muted-foreground)]">Private uploads, explicit scheduling and real analytics for your ClipForge channel. Sign-in credentials stay in the system keychain.</p>
            {connection && (
              <dl className="mt-3 space-y-1 text-xs">
                {connected || expired ? (
                  <>
                    <div className="flex gap-2"><dt className="text-[var(--muted-foreground)]">Connected as:</dt><dd className="font-semibold">{connection.channel_title}</dd></div>
                    <div className="flex gap-2"><dt className="text-[var(--muted-foreground)]">Channel ID:</dt><dd className="mono">{connection.channel_id}</dd></div>
                  </>
                ) : (
                  <div className="text-[var(--muted-foreground)]">Not connected</div>
                )}
              </dl>
            )}
          </div>
        </div>
        <div className="flex shrink-0 flex-wrap gap-2">
          {!connected && !expired && <Button size="sm" variant="accent" disabled={!clientReady || !!busy} onClick={() => void connect()}>{busy === "connect" ? <LoaderCircle className="size-3.5 animate-spin" /> : <ShieldCheck className="size-3.5" />} Connect</Button>}
          {(connected || expired) && <Button size="sm" variant={expired ? "accent" : "outline"} disabled={!clientReady || !!busy} onClick={() => void connect()}>{busy === "connect" ? <LoaderCircle className="size-3.5 animate-spin" /> : <RefreshCw className="size-3.5" />} Reconnect</Button>}
          {(connected || expired) && <Button size="sm" variant="ghost" disabled={!!busy} onClick={() => setConfirmDisconnect(true)}><LogOut className="size-3.5" /> Disconnect</Button>}
        </div>
      </div>

      {expired && <p role="alert" className="mt-4 rounded-xl bg-amber-50 px-3 py-2 text-xs text-amber-900">{connection?.error?.message ?? "The YouTube sign-in expired or was revoked."} Reconnect to continue uploads and analytics.</p>}
      {missing.length > 0 && <p className="mt-4 rounded-xl bg-amber-50 px-3 py-2 text-xs text-amber-900">Missing permission: {missing.map(([, label]) => label).join(", ")}. Reconnect and allow every requested permission.</p>}

      {confirmDisconnect && (
        <div className="mt-4 rounded-[15px] border border-red-200 bg-red-50 p-3">
          <p className="text-xs font-bold text-red-800">Disconnect YouTube?</p>
          <p className="mt-1 text-[10px] leading-4 text-red-700">The stored sign-in is revoked and removed. Your videos and ClipForge upload history stay unchanged.</p>
          <div className="mt-3 flex justify-end gap-2">
            <Button size="sm" variant="ghost" onClick={() => setConfirmDisconnect(false)} disabled={busy === "disconnect"}>Cancel</Button>
            <Button size="sm" onClick={() => void disconnect()} disabled={busy === "disconnect"} className="bg-red-700 hover:bg-red-800">{busy === "disconnect" ? <LoaderCircle className="size-3.5 animate-spin" /> : <LogOut className="size-3.5" />} Disconnect</Button>
          </div>
        </div>
      )}

      {connection && (
        <div className="cf-subtle mt-4 rounded-[15px] border p-3 text-xs">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="flex items-center gap-1.5 text-[10px] font-bold uppercase tracking-[.1em] text-[var(--muted-foreground)]"><KeyRound className="size-3" /> Google OAuth client</span>
            <span className={cn("text-[10px] font-bold", clientReady ? "text-emerald-700" : "text-amber-700")}>{clientReady ? (connection.client.client_id_source === "keyring" ? "Secure storage" : "Environment") : "Not configured"}</span>
          </div>
          <p className="mt-2 text-[11px] leading-5 text-[var(--muted-foreground)]">Register this redirect URI with your Google OAuth client: <span className="mono break-all text-[var(--foreground)]">{connection.client.redirect_uri}</span></p>
          {!editingClient && <button type="button" className="interactive-text mt-1" onClick={() => setEditingClient(true)} disabled={!!busy}><KeyRound className="size-3.5" /> {clientReady ? "Change OAuth client" : "Add OAuth client"}</button>}
          {editingClient && (
            <form className="mt-3 grid gap-2 sm:grid-cols-2" onSubmit={(event) => { event.preventDefault(); void saveClient(); }}>
              <label className="text-[11px] font-semibold">Client ID<input value={clientId} onChange={(event) => setClientId(event.target.value)} className="cf-input mt-1 text-xs" autoComplete="off" spellCheck={false} placeholder="…apps.googleusercontent.com" /></label>
              <label className="text-[11px] font-semibold">Client secret<input type="password" value={clientSecret} onChange={(event) => setClientSecret(event.target.value)} className="cf-input mt-1 text-xs" autoComplete="off" spellCheck={false} placeholder="Stored in the system keychain" /></label>
              <div className="flex justify-end gap-2 sm:col-span-2">
                <Button type="button" size="sm" variant="ghost" onClick={() => setEditingClient(false)}>Cancel</Button>
                <Button type="submit" size="sm" variant="accent" disabled={clientId.trim().length < 8 || busy === "client"}>{busy === "client" ? <LoaderCircle className="size-3.5 animate-spin" /> : <ShieldCheck className="size-3.5" />} Save securely</Button>
              </div>
            </form>
          )}
        </div>
      )}

      {connected && <YouTubePublishingSchedule />}

      {connection && <YouTubeUploadDefaults connected={connected} />}

      <p className="mt-4 text-[11px] text-[var(--muted-foreground)]">
        Uploaded videos stay in <Link href="/videos" className="underline">Videos</Link> — also after their project is deleted (a compact analytics record is kept).
      </p>

      {notice && (
        <div role={notice.tone === "error" ? "alert" : "status"} className={cn("mt-4 flex gap-2 rounded-xl px-3 py-2 text-[11px] font-medium leading-4", notice.tone === "success" && "bg-emerald-50 text-emerald-800", notice.tone === "error" && "bg-red-50 text-red-800", notice.tone === "info" && "bg-amber-50 text-amber-800")}>
          {notice.tone === "success" ? <CheckCircle2 className="mt-px size-3.5 shrink-0" /> : <AlertCircle className="mt-px size-3.5 shrink-0" />}
          {notice.text}
        </div>
      )}
    </section>
  );
}
