"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { LoaderCircle, Plus } from "lucide-react";
import { getPublishTargets } from "@/lib/api";
import type { Project } from "@/lib/types";
import { accountStatusLabel, groupTargets, initialTarget, type PublishTargets } from "@/lib/publishing";
import { PublishShell } from "./publish-shell";
import { SocialPublishSheet } from "./social-publish-sheet";
import { Alert } from "./ui/alert";
import { PublishSheet } from "./youtube-publish-sheet";

/**
 * The one Upload sheet for every entry point (Results page, Queue Overview).
 * The Platform / Account selector at the top decides the fields, metadata,
 * API and scheduling behaviour; one target account per submit.
 */
export function UploadSheet({ project, onClose, onUploaded, preferredAccountId = null }: {
  project: Project;
  onClose: () => void;
  onUploaded: () => void;
  preferredAccountId?: string | null;
}) {
  const [targets, setTargets] = useState<PublishTargets | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    getPublishTargets(project.id, controller.signal)
      .then((next) => {
        setTargets(next);
        setSelected(initialTarget(next.targets, next.initial_account_id, preferredAccountId));
      })
      .catch((reason) => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "Connected accounts could not be loaded."); });
    return () => controller.abort();
  }, [project.id, preferredAccountId]);

  const account = targets?.targets.find((item) => item.id === selected) ?? null;
  const selector = targets && targets.targets.length > 0 ? (
    <div className="flex flex-wrap items-center gap-2">
      <label htmlFor="upload-target" className="text-[11px] font-bold uppercase tracking-[.12em] text-[var(--muted-foreground)]">Platform / Account</label>
      <select id="upload-target" value={selected ?? ""} onChange={(event) => setSelected(event.target.value)} className="cf-input min-w-0 flex-1 text-sm">
        {groupTargets(targets.targets).map((group) => (
          <optgroup key={group.platform} label={group.label}>
            {group.items.map((item) => (
              <option key={item.id} value={item.id}>{item.label}{item.status !== "connected" ? ` (${accountStatusLabel(item)})` : ""}</option>
            ))}
          </optgroup>
        ))}
      </select>
      <Link href="/settings/integrations" className="interactive-text text-xs"><Plus className="size-3.5" /> Add account</Link>
    </div>
  ) : null;

  if (!targets || (targets.targets.length > 0 && !account)) {
    return (
      <PublishShell onClose={onClose}>
        <div className="grid min-h-60 place-items-center p-6 text-sm text-[var(--muted-foreground)]">
          {error ? <Alert tone="error">{error}</Alert> : <span className="flex items-center gap-2"><LoaderCircle className="size-4 animate-spin" /> Loading accounts…</span>}
        </div>
      </PublishShell>
    );
  }

  if (!account) {
    return (
      <PublishShell onClose={onClose}>
        <div className="grid min-h-60 place-items-center p-6 text-center text-sm">
          <div>
            <p className="font-semibold">No publishing account is connected.</p>
            <p className="mt-1 text-xs text-[var(--muted-foreground)]">Connect YouTube channels, Instagram professional accounts or TikTok accounts first.</p>
            <Link href="/settings/integrations" className="interactive-text mt-3 inline-flex text-sm"><Plus className="size-3.5" /> Add account</Link>
          </div>
        </div>
      </PublishShell>
    );
  }

  if (account.platform === "youtube") {
    return <PublishSheet key={account.id} project={project} accountId={account.id} selector={selector} onClose={onClose} onUploaded={onUploaded} />;
  }
  return <SocialPublishSheet key={account.id} project={project} account={account} selector={selector} onClose={onClose} onPublished={onUploaded} />;
}
