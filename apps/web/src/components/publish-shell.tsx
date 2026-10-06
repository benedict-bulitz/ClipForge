"use client";

import { X } from "lucide-react";

/**
 * The one Upload sheet frame: title "Upload", the Platform / Account selector
 * at the top, then the selected platform's own fields.
 */
export function PublishShell({ children, onClose, selector, subtitle }: { children: React.ReactNode; onClose: () => void; selector?: React.ReactNode; subtitle?: React.ReactNode }) {
  return (
    <div className="fixed inset-0 z-[80] flex justify-end bg-black/40" role="dialog" aria-modal="true" aria-labelledby="publish-sheet-title">
      <div className="flex h-full w-full max-w-[760px] flex-col border-l border-[var(--border)] bg-[var(--background)] shadow-[0_0_80px_rgba(0,0,0,.25)]">
        <header className="border-b border-[var(--border)] px-5 py-4 sm:px-6">
          <div className="flex items-center justify-between gap-3">
            <div>
              <h2 id="publish-sheet-title" className="text-lg font-semibold tracking-[-.02em]">Upload</h2>
              {subtitle && <p className="text-xs text-[var(--muted-foreground)]">{subtitle}</p>}
            </div>
            <button type="button" className="interactive-icon" onClick={onClose} aria-label="Close"><X className="size-4" /></button>
          </div>
          {selector && <div className="mt-3">{selector}</div>}
        </header>
        {children}
      </div>
    </div>
  );
}
