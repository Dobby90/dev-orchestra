import { request, version } from "./client";
import type { Project, Schema } from "./types";
const id = encodeURIComponent;
export const api = {
  deleteRun: (rid: string, revision: number) =>
    request<void>(`/runs/${id(rid)}`, {
      method: "DELETE",
      headers: version("run", rid, revision),
      body: JSON.stringify({ confirm: true }),
    }),
  session: () => request<Schema["SessionView"]>("/session"),
  login: (code: string) =>
    request<Schema["SessionView"]>("/session", {
      method: "POST",
      body: JSON.stringify({ bootstrap_code: code }),
    }),
  logout: () => request<void>("/session", { method: "DELETE" }),
  health: () => request<{ status: string }>("/health"),
  agentSettings: () => request<Schema["AgentSettingsView"]>("/settings/agents"),
  saveAgentSettings: (body: Schema["AgentSettingsInput"]) =>
    request<Schema["AgentSettingsView"]>("/settings/agents", {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  codexModels: () =>
    request<Schema["CodexModelPage"]>("/settings/agents/models", {
      method: "POST",
    }),
  agentUsage: (rid: string) =>
    request<Schema["AgentUsageView"]>(`/runs/${id(rid)}/agent-usage`),
  dashboard: () => request<Schema["DashboardView"]>("/dashboard"),
  validationProfiles: () =>
    request<Schema["ValidationCandidate"][]>("/validation-profiles"),
  detectValidation: (body: Schema["ValidationDetectInput"]) =>
    request<Schema["ValidationDetection"]>("/project-validation/detect", {
      method: "POST",
      body: JSON.stringify(body),
    }),
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
  tests: (rid: string, step?: number) =>
    request<Schema["TestResultView"]>(
      `/runs/${id(rid)}/tests${step ? `?step_id=${step}` : ""}`,
    ),
  reviews: (rid: string, step?: number) =>
    request<Schema["ReviewResultView"]>(
      `/runs/${id(rid)}/reviews${step ? `?step_id=${step}` : ""}`,
    ),
  changes: (rid: string, revision: number, candidate?: string) =>
    request<Schema["ChangesView"]>(
      `/runs/${id(rid)}/changes?revision=${revision}${candidate ? `&candidate_sha=${id(candidate)}` : ""}`,
    ),
  diff: (rid: string, file: string, candidate: string, revision: number) =>
    request<Schema["DiffView"]>(
      `/runs/${id(rid)}/diff?file_id=${id(file)}&candidate_sha=${id(candidate)}&revision=${revision}`,
    ),
  operation: (op: string) =>
    request<Schema["OperationView"]>(`/operations/${id(op)}`),
  disposition: (
    rid: string,
    issue: string,
    revision: number,
    body: Schema["DispositionInput"],
    key: string,
  ) =>
    request<Schema["OperationView"]>(
      `/runs/${id(rid)}/issues/${id(issue)}/disposition`,
      {
        method: "POST",
        headers: { ...version("run", rid, revision), "Idempotency-Key": key },
        body: JSON.stringify(body),
      },
    ),
  control: (
    rid: string,
    revision: number,
    action:
      | "pause"
      | "cancel"
      | "resume"
      | "approve"
      | "reconcile"
      | "request-changes"
      | "defer"
      | "cleanup",
    body: unknown,
    key?: string,
  ) =>
    request<Schema["OperationView"]>(`/runs/${id(rid)}/${action}`, {
      method: "POST",
      headers: {
        ...version("run", rid, revision),
        ...(key ? { "Idempotency-Key": key } : {}),
      },
      body: JSON.stringify(body),
    }),
};
