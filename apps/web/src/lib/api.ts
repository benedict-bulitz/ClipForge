import type { Project } from "./types";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000/api";
export const API_ORIGIN = API_URL.replace(/\/api\/?$/, "");

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_URL}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    throw new Error(payload?.detail ?? "ClipForge could not complete the request.");
  }
  return response.json() as Promise<T>;
}

export function createProject(
  prompt: string,
  options: Record<string, string | number | null>,
) {
  return request<Project>("/projects", {
    method: "POST",
    body: JSON.stringify({ prompt, mode: "auto", options }),
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

export function getProject(projectId: string) {
  return request<Project>(`/projects/${projectId}`, { cache: "no-store" });
}

export function listProjects() {
  return request<Project[]>("/projects", { cache: "no-store" });
}

export function renderProject(projectId: string, baseRevision: number) {
  return request<Project>(`/projects/${projectId}/render`, {
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

export function getReadiness() {
  return request<import("./types").Readiness>("/readiness", { cache: "no-store" });
}

export function mediaUrl(path: string | null) {
  return path ? `${API_ORIGIN}${path}` : null;
}
