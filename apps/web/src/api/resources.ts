import { request, version } from "./client";
import type { Project, Schema } from "./types";
const id = encodeURIComponent;
export const api = {
  session: () => request<Schema["SessionView"]>("/session"),
  login: (code: string) =>
    request<Schema["SessionView"]>("/session", {
      method: "POST",
      body: JSON.stringify({ bootstrap_code: code }),
    }),
  logout: () => request<void>("/session", { method: "DELETE" }),
  health: () => request<{ status: string }>("/health"),
  dashboard: () => request<Schema["DashboardView"]>("/dashboard"),
  projects: (cursor = "") =>
    request<Schema["ProjectPage"]>(`/projects?cursor=${id(cursor)}`),
  project: (pid: string) => request<Project>(`/projects/${id(pid)}`),
  createProject: (body: Schema["ProjectCreate"]) =>
    request<Project>("/projects", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  patchProject: (p: Project, body: Schema["ProjectPatch"]) =>
    request<Project>(`/projects/${id(p.id)}`, {
      method: "PATCH",
      headers: version("project", p.id, p.version),
      body: JSON.stringify(body),
    }),
  archiveProject: (p: Project) =>
    request<Project>(`/projects/${id(p.id)}`, {
      method: "DELETE",
      headers: version("project", p.id, p.version),
    }),
  branches: (pid: string) =>
    request<Schema["BranchPage"]>(`/projects/${id(pid)}/branches`),
  runs: (params: URLSearchParams) =>
    request<Schema["RunPage"]>(`/runs?${params}`),
  run: (rid: string) => request<Schema["RunDetail"]>(`/runs/${id(rid)}`),
  steps: (rid: string) => request<Schema["StepPage"]>(`/runs/${id(rid)}/steps`),
  report: (rid: string) =>
    request<Schema["ReportView"]>(`/runs/${id(rid)}/report`),
  spec: (rid: string) =>
    request<Schema["SpecContent"]>(`/runs/${id(rid)}/spec`),
  history: (rid: string, after: number) =>
    request<Schema["EventPage"]>(
      `/runs/${id(rid)}/event-history?after=${after}&limit=200`,
    ),
  preview: (body: Schema["Proposal"]) =>
    request<Schema["PreviewView"]>(
      `/projects/${id(body.project_id)}/run-previews`,
      { method: "POST", body: JSON.stringify(body) },
    ),
  submit: (body: Schema["RunCreate"], key: string) =>
    request<Schema["SubmissionView"]>("/runs", {
      method: "POST",
      headers: { "Idempotency-Key": key },
      body: JSON.stringify(body),
    }),
  upload: (file: File) => {
    const data = new FormData();
    data.append("file", file);
    return request<Schema["SpecView"]>("/specs", {
      method: "POST",
      body: data,
    });
  },
  control: (
    rid: string,
    revision: number,
    action: "pause" | "cancel" | "resume" | "approve" | "reconcile",
    body: unknown,
  ) =>
    request<unknown>(`/runs/${id(rid)}/${action}`, {
      method: "POST",
      headers: version("run", rid, revision),
      body: JSON.stringify(body),
    }),
};
