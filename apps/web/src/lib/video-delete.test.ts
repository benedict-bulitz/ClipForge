import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import {
  removalBlocked,
  removalDialogCopy,
  removalNotice,
  withoutVideo,
  type AnyLibraryVideo,
  type LibraryVideo,
  type SocialLibraryVideo,
  type VideoLibraryPage,
  type VideoRemoval,
} from "./videos.ts";

const read = (path: string) => readFileSync(new URL(path, import.meta.url), "utf8");
const api = read("./api.ts");
const dialog = read("../components/delete-video-dialog.tsx");
const library = read("../components/video-library.tsx");
const detail = read("../components/video-detail.tsx");

const youtube = (overrides: Partial<LibraryVideo> = {}): LibraryVideo => ({
  id: "u1", youtube_video_id: "vid00000001", title: "Why are airplane windows round?", prompt: null, topic: null,
  channel: { id: "UC1", title: "Knowledge Lab" }, state: "published", state_label: "Published", processing: false, stale: false, stale_reason: null,
  last_checked_at: null, scheduled_for: null, requested_publish_at: null, schedule_timezone: null, published_at: "2026-09-10T11:00:00Z",
  uploaded_at: "2026-09-09T10:00:00Z", sort_date: "2026-09-10T11:00:00Z", content_type: "SHORTS", duration_seconds: 10, format: null, scene_count: null,
  project: { id: "p1", available: true, title: "Why?", archived_at: null }, thumbnail_url: "/media/video-library/u1.webp",
  live_stats: null, live_stats_state: "available",
  analytics: { state: "available", fetched_at: null, views: null, engagedViews: null, averageViewDuration: null, averageViewPercentage: null, likes: null, comments: null },
  youtube_actions: { available: true, reason: null }, watch_url: null, shorts_url: null, studio_url: null,
  ...overrides,
} as LibraryVideo);

const social = (overrides: Partial<SocialLibraryVideo> = {}): SocialLibraryVideo => ({
  id: "s1", kind: "social", platform: "tiktok", title: "Round windows", caption: "", prompt: null,
  account: { id: "a1", label: "alpha", handle: "alpha", connected: true }, state: "published", status_bucket: "published", state_label: "Published",
  scheduled_for: null, schedule_timezone: null, published_at: "2026-09-10T11:00:00Z", uploaded_at: null, sort_date: "2026-09-10T11:00:00Z",
  project: { id: "p1", available: true, title: "Why?", archived_at: null }, thumbnail_url: null, remote_url: "https://www.tiktok.com/@alpha/video/1",
  remote_post_id: "1", error: null, actions: { cancel: false, reschedule: false, publish_now: false, retry: false }, privacy_level: "SELF_ONLY",
  ...overrides,
});

const removal = (overrides: Partial<VideoRemoval> = {}): VideoRemoval => ({
  id: "u1", platform: "youtube", status: "removed", removed_media: [{ kind: "library_preview", name: "u1.webp", bytes: 4000 }], freed_bytes: 4000,
  cleanup_complete: true, cancelled_schedule: false, retained: { publication_record: true, remote_post: true, project: true }, ...overrides,
});

const page = (items: AnyLibraryVideo[], overrides: Partial<VideoLibraryPage> = {}): VideoLibraryPage => ({
  items, total: items.length, limit: 24, offset: 0, next_offset: null,
  filters: { status: "all", project: "all", analytics: "all", q: "", sort: "newest", platform: "all", account: "", query: "" },
  summary: {
    total: items.length, published: items.length, unlisted: 0, scheduled: 0, private: 0, processing: 0, deleted: 0, rejected: 0,
    analytics_available: 0, analytics_processing: 0, projects_available: items.length, projects_archived: 0,
    median_average_view_percentage: null, median_average_view_percentage_n: 0,
  },
  connection: { channel_id: null, channel_title: null, status: "connected" },
  ...overrides,
});

// ---------------------------------------------------------------------------
// Confirmation copy: local copy only, never remote posts
// ---------------------------------------------------------------------------

test("the confirmation asks explicitly and says remote posts are not deleted", () => {
  const copy = removalDialogCopy(youtube());
  assert.equal(copy.title, "Delete this video from ClipForge?");
  assert.match(copy.body, /removes the local ClipForge copy/);
  assert.match(copy.body, /does NOT delete posts already published on YouTube, TikTok or Instagram/);
  assert.equal(copy.confirm, "Delete from ClipForge");
  assert.ok(copy.details.some((line) => /stays online/.test(line) && /YouTube/.test(line)));
  assert.ok(copy.details.some((line) => /Channel Performance/.test(line)));
  assert.ok(copy.details.some((line) => /project and its rendered video stay/.test(line)));
});

test("the copy matches each kind of video", () => {
  assert.ok(removalDialogCopy(youtube({ state: "scheduled" })).details.some((line) => /stays scheduled there/.test(line)));
  assert.ok(removalDialogCopy(social({ state: "published" })).details.some((line) => /stays online/.test(line) && /TikTok/.test(line)));
  assert.ok(!removalDialogCopy(social()).details.some((line) => /Channel Performance/.test(line)));
  for (const state of ["scheduled", "pending", "missed"] as const) {
    assert.ok(removalDialogCopy(social({ state, platform: "instagram" })).details.some((line) => /Instagram post has not been published yet: ClipForge cancels it/.test(line)), state);
  }
  assert.ok(removalDialogCopy(youtube({ project: { id: "p1", available: false, title: null, archived_at: "2026-09-01T00:00:00Z" } })).details.some((line) => /already deleted/.test(line)));
});

test("only a post being published right now cannot be deleted", () => {
  assert.equal(removalBlocked(youtube()), null);
  assert.equal(removalBlocked(youtube({ state: "deleted" })), null);
  for (const state of ["published", "failed", "scheduled", "pending", "missed"] as const) assert.equal(removalBlocked(social({ state })), null, state);
  for (const state of ["uploading", "processing"] as const) assert.match(removalBlocked(social({ state })) ?? "", /being published to TikTok right now/);
});

// ---------------------------------------------------------------------------
// Feedback and list update
// ---------------------------------------------------------------------------

test("success and partial cleanup use readable semantic notices", () => {
  assert.deepEqual(removalNotice(removal(), "Round windows"), { tone: "success", text: "“Round windows” was deleted from ClipForge. It is still online on YouTube." });
  assert.match(removalNotice(removal({ platform: "tiktok", cancelled_schedule: true, retained: { publication_record: true, remote_post: false, project: true } }), "X").text, /scheduled post was cancelled\.$/);
  assert.equal(removalNotice(removal({ status: "already_removed", removed_media: [] }), "X").tone, "success");
  const partial = removalNotice(removal({ cleanup_complete: false, removed_media: [] }), "X");
  assert.equal(partial.tone, "warning");
  assert.match(partial.text, /preview image could not be deleted yet/);
});

test("the deleted video leaves the list at once and the totals follow", () => {
  const items = [youtube({ id: "a" }), social({ id: "b" }), youtube({ id: "c" })];
  const next = withoutVideo(page(items, { total: 30, next_offset: 24 }), items, "b");
  assert.deepEqual(next.items.map((item) => item.id), ["a", "c"]);
  assert.equal(next.page.total, 29);
  assert.equal(next.page.next_offset, 23); // the server list shrank by one: no item is skipped by "Load more"
  assert.equal(next.page.summary.total, 2);
  // the header summary ("N published · N scheduled") follows the deleted video's own bucket and platform
  const mixed = [youtube({ id: "y", state: "scheduled" }), social({ id: "t", state: "scheduled", status_bucket: "scheduled" })];
  const base = page(mixed);
  const counted = { ...base, summary: { ...base.summary, published: 0, scheduled: 2, platforms: { youtube: 1, instagram: 0, tiktok: 1 } } };
  const afterSocial = withoutVideo(counted, mixed, "t");
  assert.equal(afterSocial.page.summary.scheduled, 1);
  assert.deepEqual(afterSocial.page.summary.platforms, { youtube: 1, instagram: 0, tiktok: 0 });
  assert.equal(counted.summary.scheduled, 2); // the previous page object is not mutated
  const failed = withoutVideo({ ...base, summary: { ...base.summary, rejected: 1 } }, [youtube({ id: "r", state: "processing_failed" })], "r");
  assert.equal(failed.page.summary.rejected, 0);
  const unchanged = withoutVideo(page(items), items, "missing");
  assert.equal(unchanged.items.length, 3);
  assert.equal(unchanged.page.total, 3);
});

// ---------------------------------------------------------------------------
// UI contract
// ---------------------------------------------------------------------------

test("the API call is an explicit DELETE of one video", () => {
  assert.match(api, /export function deleteVideo\(id: string\)/);
  assert.match(api, /`\/videos\/\$\{encodeURIComponent\(id\)\}`, \{ method: "DELETE" \}/);
  assert.doesNotMatch(api, /deleteVideos|bulkDelete/i); // single video only
});

test("Delete opens a confirmation; only the confirm button deletes", () => {
  // the row/detail buttons only open the dialog
  assert.doesNotMatch(library, /deleteVideo\(/);
  assert.doesNotMatch(detail, /deleteVideo\(/);
  assert.match(library, /<DeleteVideoButton video=\{video\} onClick=\{onDelete\} \/>/g);
  assert.equal((library.match(/<DeleteVideoButton /g) ?? []).length, 2); // YouTube and Instagram/TikTok rows
  assert.match(library, /setDeleting\(video\)/);
  assert.match(detail, /<DeleteVideoButton video=\{video\} size="page" onClick=\{\(\) => setConfirmDelete\(true\)\} \/>/);
  // the dialog is an accessible modal with an explicit confirm, cancel and Escape
  assert.match(dialog, /role="alertdialog" aria-modal="true" aria-labelledby="delete-video-title"/);
  assert.match(dialog, /async function confirm\(\) \{[\s\S]*?onDeleted\(await deleteVideo\(video\.id\)\)/);
  assert.equal((dialog.match(/deleteVideo\(/g) ?? []).length, 1);
  assert.match(dialog, /onClick=\{\(\) => void confirm\(\)\}/);
  assert.match(dialog, /onClick=\{onCancel\}[^>]*autoFocus/);
  assert.match(dialog, /event\.key === "Escape" && !deleting/);
});

test("the dialog shows loading and keeps errors visible with the shared alert", () => {
  assert.match(dialog, /disabled=\{deleting \|\| !!blocked\}/);
  assert.match(dialog, /deleting \? <LoaderCircle className="size-3\.5 animate-spin" \/>/);
  assert.match(dialog, /"Deleting…"/);
  assert.match(dialog, /<Alert tone="error" size="sm" className="mt-3">\{error\}<\/Alert>/);
  assert.match(dialog, /<Alert tone="warning" size="sm" className="mt-3">\{blocked\}<\/Alert>/);
  assert.doesNotMatch(dialog, /text-red|bg-red|text-orange|bg-yellow/);
});

test("after success the item is removed from the Videos UI and the result is announced", () => {
  assert.match(library, /function videoDeleted\(video: AnyLibraryVideo, result: VideoRemoval\) \{[\s\S]*?setDeleting\(null\);[\s\S]*?setDeleteNotice\(removalNotice\(result, video\.title\)\);[\s\S]*?withoutVideo\(page, items, video\.id\)[\s\S]*?setItems\(next\.items\)/);
  assert.match(library, /onDeleted=\{\(result\) => videoDeleted\(deleting, result\)\}/);
  assert.match(library, /<Alert tone=\{deleteNotice\.tone\}/);
  // the detail page replaces itself with the result and a way back
  assert.match(detail, /setDeleted\(removalNotice\(result, video\.title\)\)/);
  assert.match(detail, /<Alert tone=\{deleted\.tone\} size="lg" title="Deleted from ClipForge">/);
  assert.match(detail, /Back to Videos/);
});
