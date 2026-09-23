export type Scene = {
  id: string;
  start: number;
  end: number;
  narration: string;
  visual_goal: string;
  motion: string;
  asset_status: string;
  fallback_reason?: string;
  preferred_media?: "video" | "photo" | "generated_card";
  media?: {
    provider: string;
    provider_id: string;
    kind: "video" | "photo";
    cache_path?: string;
    source_url: string;
    creator: string;
    creator_url?: string | null;
    query: string;
  };
};

export type SceneMediaCandidate = {
  token: string;
  provider: string;
  provider_id: string;
  kind: "video" | "photo";
  preview_url: string;
  source_url: string;
  creator: string;
  creator_url?: string | null;
  query: string;
  width: number;
  height: number;
  duration?: number | null;
  selected?: boolean;
};

export type SceneMediaCandidates = {
  scene_number: number;
  preferred_kind: "video" | "photo";
  candidates: SceneMediaCandidate[];
};

export type BulkProjectDeletePlan = {
  project_count: number;
  project_ids: string[];
  total_bytes: number;
  total_files: number;
  total_directories: number;
  shared_cache_excluded: boolean;
};

export type BulkProjectDeleteResult = {
  deleted_projects: number;
  freed_bytes: number;
  failed_projects: Record<string, string>;
  remaining_projects: number;
};

export type ScriptBlock = { id: string; role: string; text: string };

export type PipelineStage = {
  id: string;
  label: string;
  status: "complete" | "planned" | "running" | "blocked";
};

export type ProjectState = {
  version: number;
  intent: {
    topic: string;
    intent: string;
    language: string;
    content_type: string;
    tone: string;
    research_required: boolean;
  };
  options: Record<string, string | number | boolean | null>;
  research: { required: boolean; questions: string[]; status: string; provider: string; error?: string | null; sources: Source[] };
  facts: Array<{ id: string; claim: string; confidence: number; priority: string; verification: string; sources: Source[] }>;
  script: { text: string; word_count: number; blocks: ScriptBlock[] };
  duration: { mode: string; estimated_seconds: number; actual_seconds: number | null; max_seconds: number };
  voice: { provider: string; profile: string; status: string; voice_id?: string; speed?: number; tone?: string; gender_presentation?: string; volume?: number };
  assets: { status: string; provider?: string; diagnostic?: string | null; selected_count?: number };
  scenes: Scene[];
  captions: {
    enabled?: boolean;
    style: string;
    position?: string;
    font_size: number;
    text_color?: string;
    highlight_color?: string;
    words_per_group?: number;
    timing?: string;
    alignment_provider?: string;
    diagnostic?: string | null;
    items: Array<{ text: string; start: number; end: number; timing?: string; words?: Array<{ text: string; start: number; end: number }> }>;
  };
  music: { enabled?: boolean; mood: string; volume?: number; ducking?: boolean; fades?: boolean; status: string; track?: { id: string; title: string }; selection?: { mode?: string } };
  social_metadata?: {
    status: "available" | "unavailable";
    error?: string;
    platforms: Partial<Record<"tiktok" | "instagram" | "youtube", { title?: string; description?: string; hashtags: string[]; manual?: boolean }>>;
  };
  ai_review?: {
    status: "pending" | "passed" | "passed_with_warnings" | "needs_fix" | "failed" | "unavailable";
    provider?: string | null;
    rounds: number;
    message?: string;
    items: Array<{ check: string; severity: "info" | "warning" | "error"; message: string }>;
    automatic_corrections: string[];
  };
  timeline: { duration: number; width: number; height: number; fps: number; aspect_ratio: string; cut_pace: string };
  render: { status: string; url: string | null; file_size?: number; format?: string; revision?: number; stale?: boolean; error?: string };
  export?: {
    status: "exported";
    filename: string;
    display_path: string;
    file_size: number;
    exported_at: string;
    media_url: string;
    source_revision: number;
    cleanup_status: "complete" | "warning";
    cleanup_warnings: string[];
    cleaned_directories: string[];
  };
  integrations: Record<string, string>;
  pipeline: PipelineStage[];
  edit_history: Array<{ revision: number; instruction: string; changed_components: string[]; summary: string; created_at: string }>;
};

export type Source = { label: string; url: string };

export type ReadinessItem = {
  ready: boolean;
  status: string;
  key: string | null;
  url: string;
  alternative_url?: string;
};

export type Readiness = Record<string, ReadinessItem>;

export type ProjectExport = {
  success: boolean;
  exported_filename: string;
  display_path: string;
  file_size: number;
  cleanup_status: "complete" | "warning";
  cleanup_warnings: string[];
  exported_at: string;
  media_url: string;
  already_exported: boolean;
  project: Project;
};

export type MusicTrack = {
  id: string;
  title: string;
  mood: string;
  energy: string;
  tags: string[];
  source: string;
  license: string;
  attribution: string | null;
  description: string | null;
  duration_seconds: number | null;
  preview_url: string;
};

export type GenerationJob = {
  id: string;
  project_id: string;
  prompt: string;
  base_revision: number | null;
  status: "queued" | "running" | "completed" | "failed" | "removed";
  current_stage: string;
  stage_label: string;
  progress: number;
  completed_units: number | null;
  total_units: number | null;
  started_at: string | null;
  updated_at: string;
  completed_at: string | null;
  elapsed_seconds: number;
  estimated_remaining_seconds: number | null;
  failure_category: string | null;
  failure_message: string | null;
  queue_position: number | null;
};

export type ProjectOverview = {
  id: string;
  title: string;
  status: string;
  current_revision: number | null;
  created_at: string;
  updated_at: string;
};

export type IntegrationProvider = "openai" | "brave" | "pexels";

export type IntegrationStatus =
  | "configured"
  | "connected"
  | "not_configured"
  | "invalid_credentials"
  | "rate_limited"
  | "network_error"
  | "provider_error"
  | "storage_error";

export type Integration = {
  provider: IntegrationProvider;
  configured: boolean;
  last_four: string | null;
  status: IntegrationStatus;
  source: "keyring" | "environment" | null;
  message: string | null;
};

export type EnvImportResult = {
  provider: IntegrationProvider;
  result: "imported" | "skipped";
  status: IntegrationStatus;
  configured: boolean;
  last_four: string | null;
  source: "keyring" | "environment" | null;
};

export type EnvImportResponse = { results: EnvImportResult[] };

export type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  tool_metadata: {
    provider?: string;
    tier?: "fast" | "strong";
    focus?: string | null;
    tools?: Array<{
      tool: string;
      success: boolean;
      mutating: boolean;
      revision: number | null;
    }>;
  };
  created_at: string;
};

export type ChatTurn = {
  messages: ChatMessage[];
  project: Project;
};

export type VoiceId =
  | "alloy"
  | "ash"
  | "ballad"
  | "coral"
  | "echo"
  | "fable"
  | "onyx"
  | "nova"
  | "sage"
  | "shimmer"
  | "verse"
  | "marin"
  | "cedar";

export type VoicePresentation = "neutral" | "masculine" | "feminine";
export type VoiceTone = "warm" | "calm" | "energetic" | "deep" | "documentary" | "conversational";

export type VoicePreviewRequest = {
  text?: string;
  language: "en" | "de";
  voice_id?: VoiceId;
  presentation: VoicePresentation;
  tone: VoiceTone;
  speed: number;
};

export type VoicePreview = {
  url: string;
  provider: "openai" | "macos_say";
  cached: boolean;
  cache_key: string;
};

export type Revision = {
  id: string;
  number: number;
  instruction: string;
  kind: "initial" | "user" | "system";
  changed_components: string[];
  state: ProjectState;
  created_at: string;
};

export type Project = {
  id: string;
  original_prompt: string;
  title: string;
  status: string;
  current_revision: number;
  active_tip_revision: number;
  can_undo: boolean;
  can_redo: boolean;
  created_at: string;
  updated_at: string;
  revision: Revision;
  revisions: Array<{
    id: string;
    number: number;
    parent_revision?: number | null;
    instruction: string;
    kind: "initial" | "user" | "system";
    changed_components?: string[];
    created_at: string;
    is_current: boolean;
    is_user_visible: boolean;
    on_active_branch: boolean;
  }>;
};
