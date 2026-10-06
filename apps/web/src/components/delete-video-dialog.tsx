"use client";

import { useEffect, useState } from "react";
import { LoaderCircle, Trash2 } from "lucide-react";
import { ApiError, deleteVideo } from "@/lib/api";
import { removalBlocked, removalDialogCopy, type AnyLibraryVideo, type VideoRemoval } from "@/lib/videos";
import { Alert } from "./ui/alert";
import { Button } from "./ui/button";

/** The Delete action of one Videos entry (overview row and detail page). */
export function DeleteVideoButton({ video, onClick, size = "row" }: { video: AnyLibraryVideo; onClick: () => void; size?: "row" | "page" }) {
  const blocked = removalBlocked(video);
  if (size === "page") {
    return (
      <Button variant="ghost" size="sm" onClick={onClick} disabled={!!blocked} title={blocked ?? "Delete this video from ClipForge"}>
        <Trash2 className="size-3.5" /> Delete
      </Button>
    );
  }
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={!!blocked}
      title={blocked ?? "Delete this video from ClipForge"}
      aria-label={`Delete “${video.title}” from ClipForge`}
      className="interactive-text disabled:cursor-not-allowed disabled:opacity-40"
    >
      <Trash2 className="size-3" /> Delete
    </button>
  );
}

/**
 * Explicit confirmation before deleting one video from ClipForge.  Only a
 * successful answer closes it (``onDeleted``); a failure stays visible here
 * so the user can retry or cancel.
 */
export function DeleteVideoDialog({ video, onCancel, onDeleted }: {
  video: AnyLibraryVideo;
  onCancel: () => void;
  onDeleted: (result: VideoRemoval) => void;
}) {
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const copy = removalDialogCopy(video);
  const blocked = removalBlocked(video);

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape" && !deleting) onCancel();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [deleting, onCancel]);

  async function confirm() {
    setDeleting(true);
    setError(null);
    try {
      onDeleted(await deleteVideo(video.id));
    } catch (reason) {
      setError(reason instanceof ApiError || reason instanceof Error ? reason.message : "The video could not be deleted.");
      setDeleting(false);
    }
  }

  return (
    <div className="fixed inset-0 z-[70] grid place-items-center bg-black/45 p-4" role="alertdialog" aria-modal="true" aria-labelledby="delete-video-title" aria-describedby="delete-video-body">
      <div className="w-full max-w-md rounded-[24px] border border-[var(--border)] bg-[var(--surface-elevated)] p-6 shadow-[0_22px_70px_rgba(0,0,0,.25)]">
        <h2 id="delete-video-title" className="text-lg font-semibold">{copy.title}</h2>
        <p className="mt-1 break-words text-sm font-medium">{video.title}</p>
        <p id="delete-video-body" className="mt-2 text-sm leading-6 text-[var(--muted-foreground)]">{copy.body}</p>
        <ul className="mt-3 list-disc space-y-1 pl-5 text-xs leading-5 text-[var(--muted-foreground)]">
          {copy.details.map((line) => <li key={line}>{line}</li>)}
        </ul>
        {blocked && <Alert tone="warning" size="sm" className="mt-3">{blocked}</Alert>}
        {error && <Alert tone="error" size="sm" className="mt-3">{error}</Alert>}
        <div className="mt-6 flex flex-wrap justify-end gap-2">
          <Button variant="ghost" onClick={onCancel} disabled={deleting} autoFocus>Cancel</Button>
          <Button variant="accent" onClick={() => void confirm()} disabled={deleting || !!blocked}>
            {deleting ? <LoaderCircle className="size-3.5 animate-spin" /> : <Trash2 className="size-3.5" />} {deleting ? "Deleting…" : copy.confirm}
          </Button>
        </div>
      </div>
    </div>
  );
}
