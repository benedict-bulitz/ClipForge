export type Scene = {
  id: string;
  start: number;
  end: number;
  narration: string;
  visual_goal: string;
  motion: string;
  asset_status: string;
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
  options: Record<string, string | number | null>;
  research: { required: boolean; questions: string[]; status: string; provider: string; error?: string | null; sources: Source[] };
  facts: Array<{ id: string; claim: string; confidence: number; priority: string; verification: string; sources: Source[] }>;
  script: { text: string; word_count: number; blocks: ScriptBlock[] };
  duration: { mode: string; estimated_seconds: number; actual_seconds: number | null; max_seconds: number };
  voice: { provider: string; profile: string; status: string };
  scenes: Scene[];
  captions: { style: string; font_size: number; highlight_color?: string; items: Array<{ text: string; start: number; end: number }> };
  music: { mood: string; energy: number; status: string };
  timeline: { duration: number; width: number; height: number; fps: number; aspect_ratio: string; cut_pace: string };
  render: { status: string; url: string | null; file_size?: number; format?: string };
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

export type Revision = {
  id: string;
  number: number;
  instruction: string;
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
  created_at: string;
  updated_at: string;
  revision: Revision;
  revisions: Array<{
    id: string;
    number: number;
    parent_revision?: number | null;
    instruction: string;
    changed_components?: string[];
    created_at: string;
    is_current: boolean;
  }>;
};
