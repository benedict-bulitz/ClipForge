"use client";

import { useEffect, useState } from "react";
import {
  AlertCircle,
  CheckCircle2,
  ExternalLink,
  Eye,
  EyeOff,
  FileKey2,
  Images,
  KeyRound,
  LoaderCircle,
  RefreshCw,
  Search,
  ShieldCheck,
  Sparkles,
  Trash2,
  type LucideIcon,
} from "lucide-react";
import {
  ApiError,
  deleteIntegration,
  importEnvironmentKeys,
  listIntegrations,
  saveIntegration,
  testIntegration,
} from "@/lib/api";
import type {
  EnvImportResult,
  Integration,
  IntegrationProvider,
  IntegrationStatus,
} from "@/lib/types";
import { cn } from "@/lib/utils";
import { Button } from "./ui/button";

type ProviderDetails = {
  name: string;
  description: string;
  usedFor: string;
  dashboard: string;
  icon: LucideIcon;
  iconClass: string;
};

type Notice = { tone: "success" | "error" | "info"; text: string };
type BusyAction = "save" | "test" | "delete";

const providerOrder: IntegrationProvider[] = ["openai", "brave", "pexels"];

const providers: Record<IntegrationProvider, ProviderDetails> = {
  openai: {
    name: "OpenAI",
    description: "Directs each video and creates its narration.",
    usedFor: "Planning, scripts, AI edits, and voice",
    dashboard: "https://platform.openai.com/api-keys",
    icon: Sparkles,
    iconClass: "bg-[#ff6838]/12 text-[#d94c20]",
  },
  brave: {
    name: "Brave Search",
    description: "Finds current sources for grounded explanations.",
    usedFor: "Research, citations, and fact discovery",
    dashboard: "https://api-dashboard.search.brave.com/app/keys",
    icon: Search,
    iconClass: "bg-[#34475d]/10 text-[#34475d] dark:bg-[#82a6c8]/12 dark:text-[#9fc4e8]",
  },
  pexels: {
    name: "Pexels",
    description: "Supplies relevant stock visuals for each scene.",
    usedFor: "Stock video and photography",
    dashboard: "https://www.pexels.com/api/new/",
    icon: Images,
    iconClass: "bg-[#74865f]/12 text-[#60734e] dark:bg-[#9eb584]/12 dark:text-[#b9d29d]",
  },
};

const statusLabels: Record<IntegrationStatus, string> = {
  configured: "Configured",
  connected: "Connected",
  not_configured: "Not configured",
  invalid_credentials: "Invalid key",
  rate_limited: "Rate limited",
  network_error: "Network error",
  provider_error: "Provider unavailable",
  storage_error: "Secure storage error",
};

const statusClasses: Record<IntegrationStatus, string> = {
  configured: "border-emerald-700/10 bg-emerald-50 text-emerald-700",
  connected: "border-emerald-700/10 bg-emerald-50 text-emerald-700",
  not_configured: "border-black/8 bg-black/[.035] text-[#77776d]",
  invalid_credentials: "border-red-700/10 bg-red-50 text-red-700",
  rate_limited: "border-amber-700/10 bg-amber-50 text-amber-700",
  network_error: "border-amber-700/10 bg-amber-50 text-amber-700",
  provider_error: "border-red-700/10 bg-red-50 text-red-700",
  storage_error: "border-red-700/10 bg-red-50 text-red-700",
};

export function IntegrationsSettings() {
  const [items, setItems] = useState<Integration[]>([]);
  const [loading, setLoading] = useState(true);
  const [pageError, setPageError] = useState<string | null>(null);
  const [editing, setEditing] = useState<IntegrationProvider | null>(null);
  const [candidate, setCandidate] = useState("");
  const [showCandidate, setShowCandidate] = useState(false);
  const [busy, setBusy] = useState<{ provider: IntegrationProvider; action: BusyAction } | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<IntegrationProvider | null>(null);
  const [notices, setNotices] = useState<Partial<Record<IntegrationProvider, Notice>>>({});
  const [importing, setImporting] = useState(false);
  const [importError, setImportError] = useState<string | null>(null);
  const [importResults, setImportResults] = useState<EnvImportResult[] | null>(null);

  useEffect(() => {
    let active = true;
    listIntegrations()
      .then((next) => {
        if (!active) return;
        setItems(next);
        setPageError(null);
      })
      .catch(() => {
        if (active) setPageError("ClipForge could not load integration settings.");
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, []);

  function replaceItem(next: Integration) {
    setItems((current) => current.map((item) => (item.provider === next.provider ? next : item)));
  }

  async function refreshItems() {
    try {
      setItems(await listIntegrations());
      setPageError(null);
    } catch {
      setPageError("The change succeeded, but the latest integration status could not be loaded.");
    }
  }

  function showNotice(provider: IntegrationProvider, notice: Notice) {
    setNotices((current) => ({ ...current, [provider]: notice }));
  }

  function startEditing(provider: IntegrationProvider) {
    setEditing(provider);
    setCandidate("");
    setShowCandidate(false);
    setConfirmDelete(null);
    setNotices((current) => ({ ...current, [provider]: undefined }));
  }

  function stopEditing() {
    setEditing(null);
    setCandidate("");
    setShowCandidate(false);
  }

  async function saveKey(provider: IntegrationProvider) {
    const value = candidate.trim();
    if (!value || busy) return;
    setBusy({ provider, action: "save" });
    setNotices((current) => ({ ...current, [provider]: undefined }));
    try {
      const saved = await saveIntegration(provider, value);
      replaceItem(saved);
      stopEditing();
      showNotice(provider, { tone: "success", text: "Key validated and saved securely." });
      await refreshItems();
    } catch (reason) {
      showNotice(provider, { tone: "error", text: friendlyError(reason) });
    } finally {
      setBusy(null);
    }
  }

  async function testKey(provider: IntegrationProvider) {
    if (busy) return;
    setBusy({ provider, action: "test" });
    setNotices((current) => ({ ...current, [provider]: undefined }));
    try {
      const tested = await testIntegration(provider);
      replaceItem(tested);
      showNotice(provider, noticeForTest(tested.status));
    } catch (reason) {
      showNotice(provider, { tone: "error", text: friendlyError(reason) });
    } finally {
      setBusy(null);
    }
  }

  async function removeKey(provider: IntegrationProvider) {
    if (busy) return;
    setBusy({ provider, action: "delete" });
    setNotices((current) => ({ ...current, [provider]: undefined }));
    try {
      const removed = await deleteIntegration(provider);
      replaceItem(removed);
      setConfirmDelete(null);
      showNotice(
        provider,
        removed.source === "environment"
          ? {
              tone: "info",
              text: "Secure key removed. ClipForge is still using the environment fallback.",
            }
          : { tone: "success", text: "Secure key removed." },
      );
      await refreshItems();
    } catch (reason) {
      showNotice(provider, { tone: "error", text: friendlyError(reason) });
    } finally {
      setBusy(null);
    }
  }

  async function importKeys() {
    if (importing || busy) return;
    setImporting(true);
    setImportError(null);
    try {
      const response = await importEnvironmentKeys();
      setImportResults(response.results);
      await refreshItems();
    } catch (reason) {
      setImportError(friendlyError(reason));
    } finally {
      setImporting(false);
    }
  }

  const configuredCount = items.filter((item) => item.configured).length;
  const noneConfigured = !loading && items.length > 0 && configuredCount === 0;

  return (
    <div className="mx-auto max-w-[1020px]">
      <div className="flex flex-col justify-between gap-5 sm:flex-row sm:items-end">
        <div>
          <p className="mono text-[10px] font-medium uppercase tracking-[.14em] text-[#ff6838]">Settings / Integrations</p>
          <h1 className="mt-2 text-3xl font-semibold tracking-[-.045em] sm:text-4xl">Connected services</h1>
          <p className="mt-3 max-w-2xl text-sm leading-6 text-[var(--muted-foreground)]">
            Manage the providers ClipForge uses for creative direction, trusted research, voice, and visual media.
          </p>
        </div>
        {!loading && (
          <div className="cf-surface flex shrink-0 items-center gap-2 rounded-full border px-3 py-2 text-[11px] font-bold text-[var(--muted-foreground)] shadow-sm">
            <ShieldCheck className="size-3.5 text-[#ff6838]" /> {configuredCount} of {providerOrder.length} configured
          </div>
        )}
      </div>

      {pageError && (
        <div role="alert" className="mt-6 flex items-center justify-between gap-4 rounded-[16px] border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-800">
          <span>{pageError}</span>
          <button type="button" onClick={() => void refreshItems()} className="interactive-text shrink-0 text-red-800">
            <RefreshCw className="size-3.5" /> Retry
          </button>
        </div>
      )}

      {noneConfigured && (
        <div className="mt-6 rounded-[18px] border border-[#ff6838]/20 bg-[var(--accent-soft)] px-4 py-3 text-sm leading-6 text-[var(--muted-foreground)]">
          Add only the services you want to use. Each card explains what it unlocks, and local fallbacks remain available where supported.
        </div>
      )}

      <div className="mt-7 grid items-start gap-4 xl:grid-cols-3">
        {loading
          ? providerOrder.map((provider) => <IntegrationSkeleton key={provider} />)
          : providerOrder.map((provider) => {
              const item = items.find((candidateItem) => candidateItem.provider === provider);
              return item ? (
                <IntegrationCard
                  key={provider}
                  integration={item}
                  details={providers[provider]}
                  editing={editing === provider}
                  candidate={editing === provider ? candidate : ""}
                  showCandidate={showCandidate}
                  busy={busy?.provider === provider ? busy.action : null}
                  notice={notices[provider]}
                  confirmingDelete={confirmDelete === provider}
                  onEdit={() => startEditing(provider)}
                  onCandidateChange={setCandidate}
                  onToggleCandidate={() => setShowCandidate((shown) => !shown)}
                  onSave={() => void saveKey(provider)}
                  onCancelEdit={stopEditing}
                  onTest={() => void testKey(provider)}
                  onConfirmDelete={() => {
                    setConfirmDelete(provider);
                    stopEditing();
                  }}
                  onCancelDelete={() => setConfirmDelete(null)}
                  onDelete={() => void removeKey(provider)}
                />
              ) : null;
            })}
      </div>

      <section className="cf-surface mt-6 rounded-[22px] border p-5 shadow-sm backdrop-blur sm:p-6">
        <div className="flex flex-col justify-between gap-5 sm:flex-row sm:items-center">
          <div className="flex gap-3.5">
            <div className="grid size-10 shrink-0 place-items-center rounded-[13px] bg-black/[.05] text-[#56564e]">
              <FileKey2 className="size-[18px]" />
            </div>
            <div>
              <h2 className="text-sm font-bold">Move existing keys into secure storage</h2>
              <p className="mt-1 max-w-xl text-xs leading-5 text-[#77776d]">
                Import supported keys from the current environment or <span className="mono">.env</span>. The file is read only and will not be changed or deleted.
              </p>
            </div>
          </div>
          <Button variant="outline" size="sm" onClick={() => void importKeys()} disabled={importing || !!busy} className="shrink-0">
            {importing ? <LoaderCircle className="size-3.5 animate-spin" /> : <FileKey2 className="size-3.5" />}
            {importing ? "Validating…" : "Import keys from .env"}
          </Button>
        </div>

        {importError && <p role="alert" className="mt-4 rounded-xl bg-red-50 px-3 py-2 text-xs font-medium text-red-700">{importError}</p>}
        {importResults && <ImportResults results={importResults} />}
      </section>
    </div>
  );
}

function IntegrationCard({
  integration,
  details,
  editing,
  candidate,
  showCandidate,
  busy,
  notice,
  confirmingDelete,
  onEdit,
  onCandidateChange,
  onToggleCandidate,
  onSave,
  onCancelEdit,
  onTest,
  onConfirmDelete,
  onCancelDelete,
  onDelete,
}: {
  integration: Integration;
  details: ProviderDetails;
  editing: boolean;
  candidate: string;
  showCandidate: boolean;
  busy: BusyAction | null;
  notice?: Notice;
  confirmingDelete: boolean;
  onEdit: () => void;
  onCandidateChange: (value: string) => void;
  onToggleCandidate: () => void;
  onSave: () => void;
  onCancelEdit: () => void;
  onTest: () => void;
  onConfirmDelete: () => void;
  onCancelDelete: () => void;
  onDelete: () => void;
}) {
  const Icon = details.icon;
  const storedSecurely = integration.source === "keyring";
  return (
    <article className="cf-surface overflow-hidden rounded-[22px] border shadow-sm backdrop-blur">
      <div className="p-5">
        <div className="flex items-start justify-between gap-3">
          <div className={cn("grid size-10 shrink-0 place-items-center rounded-[13px]", details.iconClass)}>
            <Icon className="size-[18px]" />
          </div>
          <span className={cn("rounded-full border px-2.5 py-1 text-[10px] font-bold", statusClasses[integration.status])}>
            {statusLabels[integration.status]}
          </span>
        </div>
        <h2 className="mt-4 text-lg font-bold tracking-[-.025em]">{details.name}</h2>
        <p className="mt-1 text-xs leading-5 text-[#77776d]">{details.description}</p>
        <p className="mt-3 text-[10px] font-bold uppercase tracking-[.08em] text-[#9a9a91]">{details.usedFor}</p>

        <div className="cf-subtle mt-5 rounded-[15px] border p-3">
          <div className="flex items-center justify-between gap-3">
            <span className="flex items-center gap-1.5 text-[10px] font-bold uppercase tracking-[.1em] text-[#88887f]">
              <KeyRound className="size-3" /> API key
            </span>
            {integration.source && (
              <span className={cn("text-[9px] font-bold", storedSecurely ? "text-emerald-700" : "text-amber-700")}>
                {storedSecurely ? "Secure storage" : "Environment fallback"}
              </span>
            )}
          </div>
          <p className="mono mt-2 text-xs font-medium text-[#4c4c45]">
            {integration.configured
              ? `•••••••••••• ${integration.last_four ?? "****"}`
              : "No key configured"}
          </p>
          {integration.source === "environment" && (
            <p className="mt-2 text-[10px] leading-4 text-amber-800">ClipForge is using the environment or .env fallback.</p>
          )}
        </div>

        {editing && (
          <form
            className="mt-4 rounded-[15px] border border-[#ff6838]/18 bg-[var(--accent-soft)] p-3"
            onSubmit={(event) => {
              event.preventDefault();
              onSave();
            }}
          >
            <label htmlFor={`${integration.provider}-key`} className="text-[10px] font-bold uppercase tracking-[.1em] text-[#77776d]">
              New API key
            </label>
            <div className="mt-2 flex items-center rounded-xl border border-[var(--border)] bg-[var(--input)] focus-within:border-[#ff6838]/45 focus-within:ring-4 focus-within:ring-[#ff6838]/5">
              <input
                id={`${integration.provider}-key`}
                type={showCandidate ? "text" : "password"}
                value={candidate}
                onChange={(event) => onCandidateChange(event.target.value)}
                autoComplete="off"
                autoCapitalize="none"
                autoCorrect="off"
                spellCheck={false}
                autoFocus
                placeholder={`Enter ${details.name} key`}
                className="min-w-0 flex-1 bg-transparent px-3 py-2.5 text-sm outline-none placeholder:text-[#aaa9a0]"
              />
              <button type="button" onClick={onToggleCandidate} className="interactive-icon size-9" aria-label={showCandidate ? "Hide API key" : "Show API key"}>
                {showCandidate ? <EyeOff className="size-3.5" /> : <Eye className="size-3.5" />}
              </button>
            </div>
            <p className="mt-2 text-[10px] leading-4 text-[#88887f]">The existing key is never loaded into this field.</p>
            <div className="mt-3 flex justify-end gap-2">
              <Button type="button" variant="ghost" size="sm" onClick={onCancelEdit} disabled={busy === "save"}>Cancel</Button>
              <Button type="submit" variant="accent" size="sm" disabled={!candidate.trim() || busy === "save"}>
                {busy === "save" ? <LoaderCircle className="size-3.5 animate-spin" /> : <ShieldCheck className="size-3.5" />}
                {busy === "save" ? "Validating…" : "Test & save"}
              </Button>
            </div>
          </form>
        )}

        {confirmingDelete && (
          <div className="mt-4 rounded-[15px] border border-red-200 bg-red-50 p-3">
            <p className="text-xs font-bold text-red-800">Remove the securely stored key?</p>
            <p className="mt-1 text-[10px] leading-4 text-red-700">An environment fallback may become active afterward.</p>
            <div className="mt-3 flex justify-end gap-2">
              <Button type="button" variant="ghost" size="sm" onClick={onCancelDelete} disabled={busy === "delete"}>Cancel</Button>
              <Button type="button" size="sm" onClick={onDelete} disabled={busy === "delete"} className="bg-red-700 hover:bg-red-800">
                {busy === "delete" ? <LoaderCircle className="size-3.5 animate-spin" /> : <Trash2 className="size-3.5" />}
                {busy === "delete" ? "Removing…" : "Remove key"}
              </Button>
            </div>
          </div>
        )}

        {notice && (
          <div
            role={notice.tone === "error" ? "alert" : "status"}
            className={cn(
              "mt-4 flex gap-2 rounded-xl px-3 py-2 text-[11px] font-medium leading-4",
              notice.tone === "success" && "bg-emerald-50 text-emerald-800",
              notice.tone === "error" && "bg-red-50 text-red-800",
              notice.tone === "info" && "bg-amber-50 text-amber-800",
            )}
          >
            {notice.tone === "success" ? <CheckCircle2 className="mt-px size-3.5 shrink-0" /> : <AlertCircle className="mt-px size-3.5 shrink-0" />}
            {notice.text}
          </div>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-1 border-t border-black/7 bg-black/[.018] px-3 py-2.5">
        <button type="button" onClick={onEdit} disabled={!!busy} className="interactive-text">
          <KeyRound className="size-3.5" /> {integration.configured ? "Change key" : "Add key"}
        </button>
        <button type="button" onClick={onTest} disabled={!integration.configured || !!busy} className="interactive-text">
          {busy === "test" ? <LoaderCircle className="size-3.5 animate-spin" /> : <RefreshCw className="size-3.5" />} Test
        </button>
        <button
          type="button"
          onClick={onConfirmDelete}
          disabled={!storedSecurely || !!busy}
          title={storedSecurely ? "Remove securely stored key" : "No securely stored key to remove"}
          className="interactive-text"
        >
          <Trash2 className="size-3.5" /> Remove
        </button>
        <a href={details.dashboard} target="_blank" rel="noreferrer" className="interactive-icon ml-auto size-8" aria-label={`Open ${details.name} API dashboard`}>
          <ExternalLink className="size-3.5" />
        </a>
      </div>
    </article>
  );
}

function ImportResults({ results }: { results: EnvImportResult[] }) {
  return (
    <div className="mt-5 border-t border-black/7 pt-4" aria-live="polite">
      <p className="mb-2 text-[10px] font-bold uppercase tracking-[.1em] text-[#88887f]">Import results</p>
      <div className="grid gap-2 sm:grid-cols-3">
        {results.map((result) => (
          <div key={result.provider} className="cf-subtle flex items-center justify-between gap-3 rounded-xl border px-3 py-2.5 text-xs">
            <span className="font-semibold">{providers[result.provider].name}</span>
            <span className={cn("font-bold", result.result === "imported" ? "text-emerald-700" : "text-[#77776d]")}>
              {importResultLabel(result)}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

function IntegrationSkeleton() {
  return (
    <div className="cf-surface h-[330px] animate-pulse rounded-[22px] border p-5">
      <div className="size-10 rounded-[13px] bg-black/[.06]" />
      <div className="mt-5 h-5 w-28 rounded bg-black/[.06]" />
      <div className="mt-3 h-3 w-4/5 rounded bg-black/[.045]" />
      <div className="mt-8 h-20 rounded-[15px] bg-black/[.04]" />
    </div>
  );
}

function friendlyError(reason: unknown): string {
  if (reason instanceof ApiError) {
    switch (reason.code) {
      case "invalid_credentials":
        return "That key was rejected. Check it and try again.";
      case "rate_limited":
        return "The provider is rate limited. Try again shortly.";
      case "network_error":
        return "The provider could not be reached. Check your connection and try again.";
      case "provider_error":
        return "The provider is temporarily unavailable. Try again later.";
      case "storage_error":
        return "Secure storage is unavailable. The key was not saved.";
    }
  }
  return "ClipForge could not complete this request. Try again.";
}

function noticeForTest(status: IntegrationStatus): Notice {
  switch (status) {
    case "connected":
      return { tone: "success", text: "Connection successful." };
    case "not_configured":
      return { tone: "info", text: "Add a key before testing this connection." };
    case "invalid_credentials":
      return { tone: "error", text: "The provider rejected the current key." };
    case "rate_limited":
      return { tone: "info", text: "The key is configured, but the provider is rate limited." };
    case "network_error":
      return { tone: "info", text: "The provider could not be reached." };
    case "provider_error":
      return { tone: "error", text: "The provider could not validate the current key." };
    case "storage_error":
      return { tone: "error", text: "Secure storage is unavailable." };
    case "configured":
      return { tone: "info", text: "The key is configured but has not been tested." };
  }
}

function importResultLabel(result: EnvImportResult): string {
  if (result.result === "imported") return "Imported";
  if (result.status === "configured") return "Already secure";
  if (result.status === "not_configured") return "No key found";
  return statusLabels[result.status];
}
