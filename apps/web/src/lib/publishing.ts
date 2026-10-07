/**
 * Multi-platform publishing (YouTube + Instagram + TikTok): types and the pure
 * rules the unified Upload sheet, Settings → Integrations, the Videos filters
 * and the Queue Overview share.  The backend re-checks everything; nothing
 * here decides what a provider accepts, it only decides what to show.
 */

export type Platform = "youtube" | "instagram" | "tiktok";
export const PLATFORMS: Platform[] = ["youtube", "instagram", "tiktok"];
export const PLATFORM_LABELS: Record<Platform, string> = { youtube: "YouTube", instagram: "Instagram", tiktok: "TikTok" };

export type Restriction = { code: string; message: string; blocks_publishing?: boolean };

/** What a provider (narrowed by the account's state) supports; the sheet renders only these. */
export type Capabilities = {
  upload: boolean;
  publish_now: boolean;
  schedule: boolean;
  schedule_mode: "native" | "clipforge";
  title: boolean;
  description: boolean;
  caption: boolean;
  caption_limit?: number;
  hashtags: boolean;
  hashtag_limit?: number;
  tags: boolean;
  thumbnail: boolean;
  cover: boolean;
  cover_mode: "frame" | "image" | null;
  privacy: boolean;
  comments: boolean;
  duet: boolean;
  stitch: boolean;
  share_to_feed: boolean;
  synthetic_media: boolean;
  ai_disclosure: boolean;
  commercial_disclosure: boolean;
  analytics: boolean;
  remote_status: boolean;
  public_post?: boolean;
  notes: string[];
};

export type PublishingAccount = {
  id: string;
  platform: Platform;
  platform_label: string;
  external_account_id: string;
  display_name: string;
  handle: string | null;
  avatar_url: string | null;
  status: "connected" | "auth_expired" | "error" | "disconnected";
  is_default: boolean;
  granted_scopes: string[];
  capabilities: Capabilities;
  restrictions: Restriction[];
  error: { code: string; message: string | null } | null;
  connected_at: string | null;
  token_expires_at?: string | null;
  profile_url?: string | null;
  label?: string;
};

export type PlatformClient = {
  configured: boolean;
  client_id_source: "keyring" | "environment" | null;
  client_secret_configured: boolean;
  redirect_uri: string;
  scopes?: string[];
  app_audited?: boolean;
  graph_version?: string;
  login_config_id?: string | null;
};

export type PlatformSection = {
  platform: Platform;
  label: string;
  client: PlatformClient;
  config: Record<string, unknown>;
  capabilities: Capabilities;
  accounts: PublishingAccount[];
  default_account_id: string | null;
};

export type SchedulerStatus = { running: boolean; last_tick_at: string | null; last_error: string | null; notice: string };

export type AccountsOverview = { platforms: Record<Platform, PlatformSection>; scheduler: SchedulerStatus };

export type ProjectPublication = {
  id: string;
  platform: Platform;
  account_id: string | null;
  account_label: string;
  state: string;
  state_label: string;
  scheduled_at: string | null;
  published_at: string | null;
  remote_url: string | null;
  render_revision: number;
  created_at: string | null;
  active: boolean;
  error?: { code: string; message: string | null } | null;
  actions?: PublicationActions;
};

export type PublishTargets = {
  targets: Array<PublishingAccount & { label: string }>;
  defaults: Record<Platform, string | null>;
  initial_account_id: string | null;
  publications: ProjectPublication[];
  scheduler: SchedulerStatus;
};

export type CreatorInfo = {
  username: string | null;
  nickname: string | null;
  avatar_url: string | null;
  privacy_level_options: string[];
  comment_disabled: boolean;
  duet_disabled: boolean;
  stitch_disabled: boolean;
  max_video_post_duration_sec: number | null;
};

export type InstagramOptions = { caption: string; hashtags: string[]; share_to_feed: boolean; cover_frame_ms: number | null };

export type TikTokOptions = {
  caption: string;
  hashtags: string[];
  privacy_level: string | null;
  allow_comments: boolean;
  allow_duet: boolean;
  allow_stitch: boolean;
  cover_frame_ms: number | null;
  is_aigc: boolean | null;
  brand_content: boolean;
  brand_organic: boolean;
  music_usage_confirmed: boolean;
};

export type SocialOptions = InstagramOptions | TikTokOptions;

export type SocialDraft = {
  account: PublishingAccount;
  platform: "instagram" | "tiktok";
  capabilities: Capabilities;
  options: Record<string, unknown>;
  metadata_source: string;
  creator_info: CreatorInfo | null;
  creator_info_error: { code: string; message: string } | null;
  ai_suggestion: { value: boolean | null; why: string };
  render_status: { uploadable: boolean; code: string | null; message: string | null };
  duration_seconds: number | null;
  caption_limit: number;
  hashtag_limit: number | null;
  scheduling: { mode: "clipforge"; notice: string; grace_minutes: number };
  music_usage_confirmation: string | null;
  app_audited: boolean | null;
  existing: SocialPublication[];
};

export type PublicationActions = { cancel: boolean; reschedule: boolean; publish_now: boolean; retry: boolean };

export type SocialPublication = {
  id: string;
  platform: "instagram" | "tiktok";
  account_id: string;
  account_label: string;
  project_id: string;
  project_title: string;
  render_revision: number;
  render_sha256: string;
  state: "pending" | "scheduled" | "uploading" | "processing" | "published" | "failed" | "cancelled" | "missed";
  state_label: string;
  mode: "now" | "schedule";
  scheduled_at: string | null;
  schedule_timezone: string | null;
  schedule_local_time: string | null;
  published_at: string | null;
  remote_url: string | null;
  remote_post_id: string | null;
  attempt_count: number;
  error: { code: string; message: string | null; diagnostics?: ProviderDiagnostics } | null;
  caption: string | null;
  privacy_level: string | null;
  actions: PublicationActions;
  requires_running_backend: boolean;
};

/** Secret-free provider diagnostics of the latest failed attempt (TikTok: provider code + log_id). */
export type ProviderDiagnostics = {
  provider_code?: string;
  provider_message?: string;
  log_id?: string;
  http_status?: number;
  context?: string;
  retryable?: boolean;
};

/** "TikTok code invalid_param · log_id 2026…" - what provider support asks for. */
export function diagnosticsLine(diagnostics: ProviderDiagnostics | undefined | null): string | null {
  if (!diagnostics) return null;
  const parts = [
    diagnostics.provider_code ? `code ${diagnostics.provider_code}` : null,
    diagnostics.http_status ? `HTTP ${diagnostics.http_status}` : null,
    diagnostics.log_id ? `log_id ${diagnostics.log_id}` : null,
  ].filter(Boolean);
  return parts.length ? `Provider ${parts.join(" · ")}` : null;
}

export type SocialPreflight = { issues: Array<{ field: string; message: string }>; ready: boolean; schedule: { status: string; message: string | null; publish_at: string | null } | null };

// ---------------------------------------------------------------------------
// Account selector
// ---------------------------------------------------------------------------

/** "YouTube · Rank Frame Shorts", "Instagram · @account", "TikTok · @account". */
export function targetLabel(account: Pick<PublishingAccount, "platform" | "display_name" | "handle">): string {
  const name = account.platform !== "youtube" && account.handle ? `@${account.handle.replace(/^@/, "")}` : account.display_name;
  return `${PLATFORM_LABELS[account.platform]} · ${name}`;
}

/**
 * The account the sheet opens with: the caller's preference, else the
 * backend's initial choice (the YouTube default), else the first connected
 * account.  Only an initial selection - the user can always switch.
 */
export function initialTarget(targets: PublishTargets["targets"], initial: string | null, preferred?: string | null): string | null {
  const usable = targets.filter((item) => item.status !== "disconnected");
  if (preferred && usable.some((item) => item.id === preferred)) return preferred;
  if (initial && usable.some((item) => item.id === initial)) return initial;
  return usable[0]?.id ?? null;
}

/** Selector groups in platform order. */
export function groupTargets<T extends { platform: Platform }>(targets: T[]): Array<{ platform: Platform; label: string; items: T[] }> {
  return PLATFORMS.map((platform) => ({ platform, label: PLATFORM_LABELS[platform], items: targets.filter((item) => item.platform === platform) })).filter((group) => group.items.length > 0);
}

// ---------------------------------------------------------------------------
// Capability-driven fields
// ---------------------------------------------------------------------------

export type SocialField = "caption" | "hashtags" | "privacy" | "comments" | "duet" | "stitch" | "cover" | "share_to_feed" | "ai_disclosure" | "commercial" | "schedule";

/** The Instagram/TikTok fields to render, in order, for these capabilities. */
export function socialFields(capabilities: Capabilities): SocialField[] {
  const fields: SocialField[] = [];
  if (capabilities.caption) fields.push("caption");
  if (capabilities.hashtags) fields.push("hashtags");
  if (capabilities.privacy) fields.push("privacy");
  if (capabilities.comments) fields.push("comments");
  if (capabilities.duet) fields.push("duet");
  if (capabilities.stitch) fields.push("stitch");
  if (capabilities.cover) fields.push("cover");
  if (capabilities.share_to_feed) fields.push("share_to_feed");
  if (capabilities.ai_disclosure) fields.push("ai_disclosure");
  if (capabilities.commercial_disclosure) fields.push("commercial");
  if (capabilities.schedule) fields.push("schedule");
  return fields;
}

export function normalizeHashtag(value: string): string | null {
  const token = value.trim();
  if (!token || /\s/.test(token)) return null;
  const tagged = token.startsWith("#") ? token : `#${token}`;
  return /^#[\p{L}\p{N}_-]+$/u.test(tagged) && tagged.length <= 80 ? tagged : null;
}

export function parseHashtags(text: string): string[] {
  const seen = new Set<string>();
  const result: string[] = [];
  for (const part of text.split(/[\s,]+/)) {
    const tag = normalizeHashtag(part);
    if (tag && !seen.has(tag.toLowerCase())) {
      seen.add(tag.toLowerCase());
      result.push(tag);
    }
  }
  return result;
}

/** The exact post text: caption plus the hashtags it does not already contain (mirrors the backend). */
export function composeCaption(caption: string, hashtags: string[]): string {
  const text = caption.replace(/\r\n/g, "\n").trim();
  const present = new Set((text.match(/#[\p{L}\p{N}_-]+/gu) ?? []).map((item) => item.toLowerCase()));
  const extra = hashtags.map(normalizeHashtag).filter((item): item is string => !!item && !present.has(item.toLowerCase()));
  const unique = extra.filter((item, index) => extra.findIndex((other) => other.toLowerCase() === item.toLowerCase()) === index);
  return unique.length ? `${text}\n\n${unique.join(" ")}`.trim() : text;
}

/** Caption length as the platform counts it (TikTok: UTF-16 units = JS string length). */
export function captionLength(text: string): number {
  return text.length;
}

export function hashtagCount(text: string): number {
  return (text.match(/#[\p{L}\p{N}_-]+/gu) ?? []).length;
}

export const PRIVACY_LABELS: Record<string, string> = {
  PUBLIC_TO_EVERYONE: "Everyone",
  MUTUAL_FOLLOW_FRIENDS: "Friends",
  FOLLOWER_OF_CREATOR: "Followers",
  SELF_ONLY: "Only me",
};

export const PUBLIC_DIRECT_POST_REQUIRES_APPROVAL = "Public Direct Post requires TikTok app approval.";

/**
 * TikTok privacy choices straight from creator_info (never a stale hardcoded
 * list).  Until the app passes TikTok's audit only "Only me" can be chosen;
 * the others stay visible but disabled with the reason - never silently downgraded.
 */
export function tiktokPrivacyChoices(creator: CreatorInfo | null, audited: boolean): Array<{ value: string; label: string; disabled: boolean; reason: string | null }> {
  if (!creator) return [];
  return creator.privacy_level_options.map((value) => {
    const blocked = !audited && value !== "SELF_ONLY";
    return { value, label: PRIVACY_LABELS[value] ?? value.replaceAll("_", " ").toLowerCase(), disabled: blocked, reason: blocked ? PUBLIC_DIRECT_POST_REQUIRES_APPROVAL : null };
  });
}

/** Interaction toggles the creator has turned off in TikTok are disabled, with the reason. */
export function tiktokInteraction(creator: CreatorInfo | null, kind: "comments" | "duet" | "stitch"): { disabled: boolean; reason: string | null } {
  const off = !creator || (kind === "comments" ? creator.comment_disabled : kind === "duet" ? creator.duet_disabled : creator.stitch_disabled);
  return { disabled: off, reason: !creator ? "TikTok's creator settings are not loaded." : off ? `${kind[0].toUpperCase()}${kind.slice(1)} is turned off for this account in TikTok.` : null };
}

/** Fast, local checks so the button explains itself; the backend preflight is the authority. */
export function socialIssues(draft: SocialDraft, options: Record<string, unknown>, mode: "now" | "schedule", scheduleReady: boolean): string[] {
  const issues: string[] = [];
  if (!draft.render_status.uploadable) issues.push(draft.render_status.message ?? "Render this revision first.");
  if (!draft.capabilities.upload) issues.push(draft.account.restrictions.find((item) => item.blocks_publishing)?.message ?? "This account cannot publish right now. Reconnect it in Settings → Integrations.");
  const caption = composeCaption(String(options.caption ?? ""), (options.hashtags as string[]) ?? []);
  if (captionLength(caption) > draft.caption_limit) issues.push(`The caption is longer than ${draft.caption_limit} characters.`);
  if (draft.hashtag_limit && hashtagCount(caption) > draft.hashtag_limit) issues.push(`Use at most ${draft.hashtag_limit} hashtags.`);
  if (draft.platform === "tiktok") {
    if (!options.privacy_level) issues.push("Choose who can view this video.");
    if (options.is_aigc === null || options.is_aigc === undefined) issues.push("Answer the AI-generated content question.");
    if (!options.music_usage_confirmed) issues.push("Confirm TikTok's Music Usage Confirmation.");
  }
  if (mode === "schedule" && !scheduleReady) issues.push("Choose a valid date and time.");
  return issues;
}

/** The publish sheet's primary label: always "Upload" (or "Schedule" for a timed post). */
export function socialSubmitLabel(mode: "now" | "schedule"): string {
  return mode === "schedule" ? "Schedule" : "Upload";
}

// ---------------------------------------------------------------------------
// Publication state
// ---------------------------------------------------------------------------

export type Tone = "busy" | "success" | "warn" | "error" | "muted";

export function publicationTone(state: string): Tone {
  if (state === "published") return "success";
  if (state === "uploading" || state === "processing" || state === "pending") return "busy";
  if (state === "scheduled") return "muted";
  if (state === "missed") return "warn";
  if (state === "failed") return "error";
  return "muted";
}

/** A compact line for Queue Overview / project cards: "TikTok · @alpha: Scheduled". */
export function publicationSummary(item: Pick<ProjectPublication, "platform" | "account_label" | "state_label">): string {
  return `${PLATFORM_LABELS[item.platform]} · ${item.account_label}: ${item.state_label}`;
}

export function activePublications<T extends { state: string }>(items: T[]): T[] {
  return items.filter((item) => item.state !== "cancelled");
}

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------

export const CAPABILITY_CHIPS: Array<[keyof Capabilities, string]> = [
  ["upload", "Upload"],
  ["schedule", "Schedule"],
  ["caption", "Caption"],
  ["title", "Title"],
  ["thumbnail", "Thumbnail"],
  ["cover", "Cover frame"],
  ["privacy", "Privacy"],
  ["analytics", "Analytics"],
  ["remote_status", "Status"],
];

export function capabilityChips(capabilities: Capabilities): string[] {
  return CAPABILITY_CHIPS.filter(([key]) => capabilities[key] === true).map(([, label]) => label);
}

export function accountStatusLabel(account: Pick<PublishingAccount, "status">): string {
  return ({ connected: "Connected", auth_expired: "Reconnect needed", error: "Error", disconnected: "Disconnected" } as Record<string, string>)[account.status] ?? account.status;
}

/** OAuth callback query → notice (?platform=tiktok&result=connected|error&reason=…, or YouTube's ?youtube=…). */
/**
 * Settings → Integrations card state on every fresh mount: all collapsed.
 * Never derived from configuration, accounts, the default account, an OAuth
 * result or the URL - the user opens what they need.
 */
export function initialCardState(): Record<Platform, boolean> {
  return Object.fromEntries(PLATFORMS.map((platform) => [platform, false])) as Record<Platform, boolean>;
}

/** Manual expand/collapse of one card; the other cards keep their state. */
export function toggleCard(state: Record<Platform, boolean>, platform: Platform): Record<Platform, boolean> {
  return { ...state, [platform]: !state[platform] };
}

export function callbackNotice(params: URLSearchParams): { platform: Platform; tone: "success" | "error"; text: string } | null {
  const youtube = params.get("youtube");
  const platform = (youtube ? "youtube" : params.get("platform")) as Platform | null;
  const result = youtube ?? params.get("result");
  if (!platform || !PLATFORMS.includes(platform) || !result) return null;
  const label = PLATFORM_LABELS[platform];
  if (result === "connected") {
    const count = Number(params.get("count") ?? "1");
    return { platform, tone: "success", text: platform === "instagram" && count > 1 ? `${count} Instagram accounts connected.` : `${label} account connected.` };
  }
  const reason = params.get("reason") ?? "";
  return { platform, tone: "error", text: CALLBACK_REASONS[reason] ?? `${label} could not be connected. Try again.` };
}

const CALLBACK_REASONS: Record<string, string> = {
  access_denied: "Sign-in was cancelled. Nothing was connected.",
  invalid_state: "The sign-in link expired or was already used. Start again.",
  no_channel: "This Google account has no YouTube channel.",
  no_refresh_token: "The provider did not grant offline access. Remove ClipForge from the account's app permissions and connect again.",
  storage_error: "Secure storage is unavailable, so nothing was connected.",
  api_disabled: "Enable the YouTube Data API v3 and YouTube Analytics API for your Google Cloud project.",
  no_professional_account: "No Instagram professional (Business or Creator) account linked to a Facebook Page was shared. Personal Instagram accounts cannot publish through Meta's API.",
  client_not_configured: "Add the developer app credentials first.",
  insufficient_scope: "A required permission was not granted. Connect again and allow every requested permission.",
};
