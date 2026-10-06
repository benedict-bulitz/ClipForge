import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import {
  PUBLIC_DIRECT_POST_REQUIRES_APPROVAL,
  callbackNotice,
  capabilityChips,
  composeCaption,
  groupTargets,
  hashtagCount,
  initialTarget,
  parseHashtags,
  publicationTone,
  socialFields,
  socialIssues,
  socialSubmitLabel,
  targetLabel,
  tiktokInteraction,
  tiktokPrivacyChoices,
  type Capabilities,
  type CreatorInfo,
  type PublishingAccount,
  type SocialDraft,
} from "./publishing.ts";
import { DEFAULT_FILTERS, accountFilterOptions, libraryQuery, parseLibraryFilters, sameFilters } from "./videos.ts";

const read = (path: string) => readFileSync(new URL(path, import.meta.url), "utf8");
const settings = read("../components/publishing-integrations.tsx");
const integrations = read("../components/integrations-settings.tsx");
const uploadSheet = read("../components/upload-sheet.tsx");
const socialSheet = read("../components/social-publish-sheet.tsx");
const youtubeSheet = read("../components/youtube-publish-sheet.tsx");
const shell = read("../components/publish-shell.tsx");
const panel = read("../components/youtube-panel.tsx");
const workspace = read("../components/project-workspace.tsx");
const queue = read("../components/queue-overview.tsx");
const library = read("../components/video-library.tsx");
const api = read("./api.ts");

const BASE: Capabilities = {
  upload: true, publish_now: true, schedule: true, schedule_mode: "clipforge", title: false, description: false, caption: true, caption_limit: 2200,
  hashtags: true, tags: false, thumbnail: false, cover: true, cover_mode: "frame", privacy: false, comments: false, duet: false, stitch: false,
  share_to_feed: false, synthetic_media: false, ai_disclosure: false, commercial_disclosure: false, analytics: false, remote_status: true, notes: [],
};
const INSTAGRAM: Capabilities = { ...BASE, share_to_feed: true, hashtag_limit: 30 };
const TIKTOK: Capabilities = { ...BASE, privacy: true, comments: true, duet: true, stitch: true, ai_disclosure: true, commercial_disclosure: true, public_post: false };
const YOUTUBE: Capabilities = { ...BASE, schedule_mode: "native", title: true, description: true, caption: false, tags: true, thumbnail: true, cover: false, cover_mode: null, privacy: true, synthetic_media: true, analytics: true };

function account(id: string, platform: PublishingAccount["platform"], extra: Partial<PublishingAccount> = {}): PublishingAccount & { label: string } {
  const base: PublishingAccount = {
    id, platform, platform_label: platform, external_account_id: `ext-${id}`, display_name: `Name ${id}`, handle: platform === "youtube" ? null : `handle_${id}`,
    avatar_url: null, status: "connected", is_default: false, granted_scopes: [], capabilities: platform === "youtube" ? YOUTUBE : platform === "instagram" ? INSTAGRAM : TIKTOK,
    restrictions: [], error: null, connected_at: null, ...extra,
  };
  return { ...base, label: targetLabel(base) };
}

const CREATOR: CreatorInfo = {
  username: "alpha", nickname: "Alpha", avatar_url: null, privacy_level_options: ["PUBLIC_TO_EVERYONE", "FOLLOWER_OF_CREATOR", "SELF_ONLY"],
  comment_disabled: false, duet_disabled: true, stitch_disabled: false, max_video_post_duration_sec: 600,
};

// ---------------------------------------------------------------------------
// Account selector
// ---------------------------------------------------------------------------

test("account selector labels: YouTube by channel name, Instagram/TikTok by handle", () => {
  assert.equal(targetLabel({ platform: "youtube", display_name: "Rank Frame Shorts", handle: null }), "YouTube · Rank Frame Shorts");
  assert.equal(targetLabel({ platform: "instagram", display_name: "Brand", handle: "brand_one" }), "Instagram · @brand_one");
  assert.equal(targetLabel({ platform: "tiktok", display_name: "Alpha", handle: "@alpha" }), "TikTok · @alpha");
});

test("the default account is only the initial selection", () => {
  const targets = [account("y1", "youtube"), account("y2", "youtube"), account("i1", "instagram"), account("t1", "tiktok"), account("t2", "tiktok", { status: "disconnected" })];
  assert.equal(initialTarget(targets, "y2"), "y2");
  assert.equal(initialTarget(targets, "y2", "t1"), "t1"); // the caller's choice wins
  assert.equal(initialTarget(targets, "y2", "t2"), "y2"); // never a disconnected account
  assert.equal(initialTarget(targets, null), "y1");
  assert.equal(initialTarget([], null), null);
  assert.deepEqual(groupTargets(targets).map((group) => [group.platform, group.items.length]), [["youtube", 2], ["instagram", 1], ["tiktok", 2]]);
});

// ---------------------------------------------------------------------------
// Capability-driven fields and platform metadata
// ---------------------------------------------------------------------------

test("fields follow capabilities: no YouTube title for Instagram/TikTok, no controls a provider cannot support", () => {
  assert.deepEqual(socialFields(INSTAGRAM), ["caption", "hashtags", "cover", "share_to_feed", "schedule"]);
  assert.deepEqual(socialFields(TIKTOK), ["caption", "hashtags", "privacy", "comments", "duet", "stitch", "cover", "ai_disclosure", "commercial", "schedule"]);
  assert.deepEqual(socialFields({ ...INSTAGRAM, cover: false, schedule: false }), ["caption", "hashtags", "share_to_feed"]);
  assert.ok(!socialFields(INSTAGRAM).includes("privacy"));
  // The sheet renders only what socialFields returns, and never a title input for these platforms.
  assert.match(socialSheet, /const fields = socialFields\(caps\)/);
  assert.match(socialSheet, /fields\.includes\("privacy"\)/);
  assert.doesNotMatch(socialSheet, /aria-label="Title"|Video title/);
});

test("caption mapping: hashtags join the caption once, in order", () => {
  assert.equal(composeCaption("Why windows are round.", ["#aviation", "learn", "#Aviation"]), "Why windows are round.\n\n#aviation #learn");
  assert.equal(composeCaption("Already #aviation inside", ["#aviation", "#planes"]), "Already #aviation inside\n\n#planes");
  assert.equal(composeCaption("Only text", []), "Only text");
  assert.deepEqual(parseHashtags("#a, b #c  #a bad#tag"), ["#a", "#b", "#c"]);
  assert.equal(hashtagCount("x #a #b"), 2);
});

test("TikTok privacy comes from creator_info; public needs app approval and is never downgraded", () => {
  const unaudited = tiktokPrivacyChoices(CREATOR, false);
  assert.deepEqual(unaudited.map((item) => [item.value, item.disabled]), [["PUBLIC_TO_EVERYONE", true], ["FOLLOWER_OF_CREATOR", true], ["SELF_ONLY", false]]);
  assert.equal(unaudited[0].reason, PUBLIC_DIRECT_POST_REQUIRES_APPROVAL);
  assert.equal(PUBLIC_DIRECT_POST_REQUIRES_APPROVAL, "Public Direct Post requires TikTok app approval.");
  assert.ok(tiktokPrivacyChoices(CREATOR, true).every((item) => !item.disabled));
  assert.deepEqual(tiktokPrivacyChoices({ ...CREATOR, privacy_level_options: ["SELF_ONLY"] }, true).map((item) => item.value), ["SELF_ONLY"]);
  assert.deepEqual(tiktokPrivacyChoices(null, true), []);
  assert.deepEqual(tiktokInteraction(CREATOR, "duet"), { disabled: true, reason: "Duet is turned off for this account in TikTok." });
  assert.equal(tiktokInteraction(CREATOR, "comments").disabled, false);
});

test("local issues explain a blocked submit; the label is Upload or Schedule", () => {
  const draft = { render_status: { uploadable: true, code: null, message: null }, capabilities: TIKTOK, account: account("t1", "tiktok"), caption_limit: 2200, hashtag_limit: null, platform: "tiktok" } as unknown as SocialDraft;
  assert.deepEqual(socialIssues(draft, { caption: "x", hashtags: [], privacy_level: null, is_aigc: null, music_usage_confirmed: false }, "now", true), [
    "Choose who can view this video.", "Answer the AI-generated content question.", "Confirm TikTok's Music Usage Confirmation.",
  ]);
  assert.deepEqual(socialIssues(draft, { caption: "x", hashtags: [], privacy_level: "SELF_ONLY", is_aigc: true, music_usage_confirmed: true }, "schedule", false), ["Choose a valid date and time."]);
  const blocked = { ...draft, capabilities: { ...TIKTOK, upload: false }, account: account("t1", "tiktok", { restrictions: [{ code: "insufficient_scope", message: "Reconnect and allow posting.", blocks_publishing: true }] }) } as SocialDraft;
  assert.ok(socialIssues(blocked, { caption: "x", privacy_level: "SELF_ONLY", is_aigc: false, music_usage_confirmed: true }, "now", true).includes("Reconnect and allow posting."));
  const instagram = { ...draft, platform: "instagram", capabilities: INSTAGRAM, hashtag_limit: 30 } as SocialDraft;
  assert.deepEqual(socialIssues(instagram, { caption: "x", hashtags: Array.from({ length: 31 }, (_, index) => `#t${index}`) }, "now", true), ["Use at most 30 hashtags."]);
  assert.equal(socialSubmitLabel("now"), "Upload");
  assert.equal(socialSubmitLabel("schedule"), "Schedule");
  assert.equal(publicationTone("missed"), "warn");
  assert.equal(publicationTone("published"), "success");
});

// ---------------------------------------------------------------------------
// Unified Upload flow
// ---------------------------------------------------------------------------

test("every entry point says Upload and opens the one unified sheet", () => {
  assert.match(workspace, />Upload<\/span>/);
  assert.doesNotMatch(workspace + panel + queue, /Upload to YouTube/);
  assert.match(panel, /<UploadSheet/);
  assert.match(panel, /<Upload className="size-3\.5" \/> Upload/);
  assert.match(queue, /<UploadSheet project=\{publishing\}/);
  assert.match(shell, /id="publish-sheet-title"[^>]*>Upload</);
});

test("the Platform / Account selector decides which sheet (and account) is used", () => {
  assert.match(uploadSheet, /Platform \/ Account/);
  assert.match(uploadSheet, /getPublishTargets\(project\.id/);
  assert.match(uploadSheet, /account\.platform === "youtube"/);
  assert.match(uploadSheet, /<PublishSheet key=\{account\.id\} project=\{project\} accountId=\{account\.id\}/);
  assert.match(uploadSheet, /<SocialPublishSheet key=\{account\.id\} project=\{project\} account=\{account\}/);
  // Every YouTube request of the sheet names the selected channel.
  assert.match(youtubeSheet, /getPublishingDraft\(project\.id, region, language, detectTimeZone\(\), accountId\)/);
  assert.match(youtubeSheet, /preflightYouTubeUpload\(project\.id, payload, region, language, controller\.signal, accountId\)/);
  assert.match(youtubeSheet, /keepConflict, accountId,/);
  assert.match(api, /account_id: accountId \?\? null, options, region, language, force_new/);
});

test("ClipForge-owned scheduling says it needs the backend running", () => {
  assert.match(socialSheet, /Runs only while the ClipForge backend is running with internet access/);
  assert.match(socialSheet, /draft\.scheduling\.notice/);
  assert.match(socialSheet, /Instagram has no separate title/);
  assert.match(socialSheet, /TikTok shows this as the video's caption/);
});

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------

test("Settings shows expandable YouTube / Instagram / TikTok sections with any number of account cards", () => {
  assert.match(integrations, /<PublishingIntegrations \/>/);
  assert.match(settings, /PLATFORMS\.map\(\(platform\) => \(\s*<PlatformCard/);
  assert.match(settings, /aria-expanded=\{open\}/);
  assert.match(settings, /section\.accounts\.map\(\(account\) => \(\s*<AccountCard/);
  assert.match(settings, /Add account/);
  assert.match(settings, /Reconnect/);
  assert.match(settings, /Disconnect/);
  assert.match(settings, /Make default/);
  assert.match(settings, /account\.restrictions\.map/);
  assert.match(settings, /type="password"/);
  // Credentials are never rendered: only "Configured".
  assert.doesNotMatch(settings, /client_secret\b(?!_configured)/);
  assert.doesNotMatch(settings, /localStorage/);
  assert.match(settings, /Public Direct Post requires TikTok app approval/);
});

test("capability chips and OAuth callback notices", () => {
  assert.deepEqual(capabilityChips(YOUTUBE), ["Upload", "Schedule", "Title", "Thumbnail", "Privacy", "Analytics", "Status"]);
  assert.deepEqual(capabilityChips(INSTAGRAM), ["Upload", "Schedule", "Caption", "Cover frame", "Status"]);
  assert.deepEqual(callbackNotice(new URLSearchParams("platform=tiktok&result=connected&account=x")), { platform: "tiktok", tone: "success", text: "TikTok account connected." });
  assert.deepEqual(callbackNotice(new URLSearchParams("platform=instagram&result=connected&count=2"))?.text, "2 Instagram accounts connected.");
  assert.match(callbackNotice(new URLSearchParams("platform=instagram&result=error&reason=no_professional_account"))?.text ?? "", /professional/);
  assert.equal(callbackNotice(new URLSearchParams("youtube=connected"))?.platform, "youtube");
  assert.equal(callbackNotice(new URLSearchParams("")), null);
});

// ---------------------------------------------------------------------------
// Videos tab
// ---------------------------------------------------------------------------

test("Videos filters: platform and account round-trip through the URL", () => {
  const filters = parseLibraryFilters({ platform: "tiktok", account: "3f0b6a1e-0000-4000-8000-000000000001", status: "failed" });
  assert.equal(filters.platform, "tiktok");
  assert.equal(filters.account, "3f0b6a1e-0000-4000-8000-000000000001");
  assert.equal(filters.status, "failed");
  assert.equal(libraryQuery(filters), "?status=failed&platform=tiktok&account=3f0b6a1e-0000-4000-8000-000000000001");
  assert.equal(parseLibraryFilters({ platform: "myspace", account: "../x" }).platform, "all");
  assert.equal(parseLibraryFilters({ account: "../x" }).account, "");
  assert.equal(libraryQuery(DEFAULT_FILTERS), "");
  assert.equal(sameFilters(DEFAULT_FILTERS, { ...DEFAULT_FILTERS, account: "x".repeat(10) }), false);
  const accounts = [account("y1", "youtube", { display_name: "Rank Frame Shorts" }), account("t1", "tiktok"), account("i1", "instagram", { status: "disconnected" })];
  assert.deepEqual(accountFilterOptions(accounts, "all").map(([, label]) => label), ["All accounts", "YouTube · Rank Frame Shorts", "TikTok · @handle_t1", "Instagram · @handle_i1 (disconnected)"]);
  assert.deepEqual(accountFilterOptions(accounts, "tiktok").map(([id]) => id), ["", "t1"]);
  assert.match(library, /label="Platform"/);
  assert.match(library, /label="Account"/);
  assert.match(library, /isSocialVideo\(video\) \? <SocialVideoRow/);
  assert.match(api, /"\/publishing\/projects\/\$\{projectId\}\/targets"|`\/publishing\/projects\/\$\{projectId\}\/targets`/);
});
