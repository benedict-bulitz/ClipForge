import type {
  EnvImportResponse,
  ChatMessage,
  ChatTurn,
  Integration,
  IntegrationProvider,
  GenerationJob,
  Project,
  ProjectOverview,
  ProjectExport,
  VoicePreview,
  VoicePreviewRequest,
  SceneMediaCandidates,
  BulkProjectDeletePlan,
  BulkProjectDeleteResult,
  MusicTrack,
} from "./types";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000/api";
export const API_ORIGIN = API_URL.replace(/\/api\/?$/, "");

type ApiErrorDetail = {
  status?: string;
  message?: string;
};

export class ApiError extends Error {
  constructor(
    message: string,
    readonly statusCode: number,
    readonly code?: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_URL}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  if (!response.ok) {
    const payload: unknown = await response.json().catch(() => null);
    const detail =
      payload && typeof payload === "object" && "detail" in payload
        ? (payload as { detail?: unknown }).detail
        : null;
    if (typeof detail === "string") {
      throw new ApiError(detail, response.status);
    }
    if (detail && typeof detail === "object") {
      const structured = detail as ApiErrorDetail;
      throw new ApiError(
        typeof structured.message === "string"
          ? structured.message
          : "ClipForge could not complete the request.",
        response.status,
        typeof structured.status === "string" ? structured.status : undefined,
      );
    }
    throw new ApiError("ClipForge could not complete the request.", response.status);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export function createProject(
  prompt: string,
  options: Record<string, string | number | boolean | null>,
) {
  return request<Project>("/projects", {
    method: "POST",
    body: JSON.stringify({ prompt, mode: "auto", options }),
  });
}

export function startGeneration(
  prompt: string,
  options: Record<string, string | number | boolean | null>,
) {
  return request<GenerationJob>("/generation-jobs", {
    method: "POST",
    body: JSON.stringify({ prompt, mode: "auto", options }),
  });
}

export function getGenerationJob(jobId: string, signal?: AbortSignal) {
  return request<GenerationJob>(`/generation-jobs/${jobId}`, {
    cache: "no-store",
    signal,
  });
}

export function listGenerationJobs(signal?: AbortSignal) {
  return request<GenerationJob[]>("/generation-jobs", { cache: "no-store", signal });
}

export function removeQueuedGenerationJob(jobId: string) {
  return request<void>(`/generation-jobs/${jobId}`, { method: "DELETE" });
}

export function clearGenerationQueue() {
  return request<void>("/generation-jobs/queue", { method: "DELETE" });
}

export function getActiveGenerationJob(signal?: AbortSignal) {
  return request<GenerationJob | null>("/generation-jobs/active", {
    cache: "no-store",
    signal,
  });
}

export function editProject(projectId: string, instruction: string) {
  return request<Project>(`/projects/${projectId}/edits`, {
    method: "POST",
    body: JSON.stringify({ instruction }),
  });
}

export function editProjectAtRevision(projectId: string, instruction: string, baseRevision: number) {
  return request<Project>(`/projects/${projectId}/edits`, {
    method: "POST",
    body: JSON.stringify({ instruction, base_revision: baseRevision }),
  });
}

export function getSceneMediaCandidates(projectId: string, sceneNumber: number) {
  return request<SceneMediaCandidates>(`/projects/${projectId}/scenes/${sceneNumber}/media-candidates`, { cache: "no-store" });
}

export function applySceneMediaCandidate(projectId: string, sceneNumber: number, token: string, baseRevision: number) {
  return request<Project>(`/projects/${projectId}/scenes/${sceneNumber}/media-candidates/apply`, {
    method: "POST",
    body: JSON.stringify({ token, base_revision: baseRevision }),
  });
}

export function getProjectChat(projectId: string) {
  return request<ChatMessage[]>(`/projects/${projectId}/chat`, { cache: "no-store" });
}

export function sendProjectMessage(projectId: string, message: string) {
  return request<ChatTurn>(`/projects/${projectId}/chat`, {
    method: "POST",
    body: JSON.stringify({ message }),
  });
}

export function previewVoice(payload: VoicePreviewRequest, signal?: AbortSignal) {
  return request<VoicePreview>("/voice/preview", {
    method: "POST",
    body: JSON.stringify(payload),
    signal,
  });
}

export function getProject(projectId: string) {
  return request<Project>(`/projects/${projectId}`, { cache: "no-store" });
}

export function deleteProject(projectId: string) {
  return request<void>(`/projects/${projectId}`, { method: "DELETE" });
}

export function updateProjectAudio(projectId: string, baseRevision: number, audio: { voice_volume: number; music_volume: number; music_enabled: boolean }) {
  return request<Project>(`/projects/${projectId}/audio`, {
    method: "PATCH",
    body: JSON.stringify({ base_revision: baseRevision, ...audio }),
  });
}

export function listProjectMusicTracks(projectId: string, mode: "ai_matched" | "all_music") {
  return request<{ mode: "ai_matched" | "all_music"; tracks: MusicTrack[] }>(`/projects/${projectId}/music/tracks?mode=${mode}`, { cache: "no-store" });
}

export function updateProjectMusicSelection(projectId: string, baseRevision: number, trackId: string | null, mode: "ai_matched" | "all_music") {
  return request<Project>(`/projects/${projectId}/music`, {
    method: "PATCH",
    body: JSON.stringify({ base_revision: baseRevision, track_id: trackId, mode }),
  });
}

export function updateProjectSocialMetadata(projectId: string, baseRevision: number, hashtags: Record<"tiktok" | "instagram" | "youtube", string[]>, metadata?: Record<"tiktok" | "instagram" | "youtube", { title: string; description: string }>) {
  return request<Project>(`/projects/${projectId}/social-metadata`, {
    method: "PATCH",
    body: JSON.stringify({ base_revision: baseRevision, hashtags, metadata }),
  });
}

export function generateProjectSocialMetadata(projectId: string, baseRevision: number, platform?: "tiktok" | "instagram" | "youtube") {
  return request<Project>(`/projects/${projectId}/social-metadata/generate`, {
    method: "POST",
    body: JSON.stringify({ base_revision: baseRevision, platform: platform ?? null }),
  });
}

export function generateProjectThumbnails(projectId: string, baseRevision: number) {
  return request<Project>(`/projects/${projectId}/thumbnails/generate`, {
    method: "POST",
    body: JSON.stringify({ base_revision: baseRevision }),
  });
}

export function selectProjectThumbnail(projectId: string, baseRevision: number, variantId: string) {
  return request<Project>(`/projects/${projectId}/thumbnails`, {
    method: "PATCH",
    body: JSON.stringify({ base_revision: baseRevision, variant_id: variantId }),
  });
}

export function listProjects() {
  return request<Project[]>("/projects", { cache: "no-store" });
}

export function listProjectOverview() {
  return request<ProjectOverview[]>("/projects/overview", { cache: "no-store" });
}

export function getProjectGenerationJob(projectId: string) {
  return request<GenerationJob>(`/generation-jobs/projects/${projectId}`, { cache: "no-store" });
}

export function getBulkProjectDeletePlan() {
  return request<BulkProjectDeletePlan>("/projects/delete-plan", { cache: "no-store" });
}

export function deleteAllProjects() {
  return request<BulkProjectDeleteResult>("/projects", { method: "DELETE" });
}

export function renderProject(projectId: string, baseRevision: number) {
  return request<Project>(`/projects/${projectId}/render`, {
    method: "POST",
    body: JSON.stringify({ base_revision: baseRevision }),
  });
}

export function exportProject(projectId: string, baseRevision: number) {
  return request<ProjectExport>(`/projects/${projectId}/export`, {
    method: "POST",
    body: JSON.stringify({ base_revision: baseRevision }),
  });
}

export function undoProject(projectId: string, baseRevision: number) {
  return request<Project>(`/projects/${projectId}/undo`, {
    method: "POST",
    body: JSON.stringify({ base_revision: baseRevision }),
  });
}

export function redoProject(projectId: string, baseRevision: number) {
  return request<Project>(`/projects/${projectId}/redo`, {
    method: "POST",
    body: JSON.stringify({ base_revision: baseRevision }),
  });
}

export function getReadiness() {
  return request<import("./types").Readiness>("/readiness", { cache: "no-store" });
}

export function listIntegrations() {
  return request<Integration[]>("/settings/integrations", { cache: "no-store" });
}

export function saveIntegration(provider: IntegrationProvider, apiKey: string) {
  return request<Integration>(`/settings/integrations/${provider}`, {
    method: "POST",
    body: JSON.stringify({ api_key: apiKey }),
  });
}

export function testIntegration(provider: IntegrationProvider) {
  return request<Integration>(`/settings/integrations/${provider}/test`, {
    method: "POST",
  });
}

export function deleteIntegration(provider: IntegrationProvider) {
  return request<Integration>(`/settings/integrations/${provider}`, {
    method: "DELETE",
  });
}

export function importEnvironmentKeys() {
  return request<EnvImportResponse>("/settings/integrations/import-env", {
    method: "POST",
  });
}

export function mediaUrl(path: string | null) {
  return path ? `${API_ORIGIN}${path}` : null;
}
