import { request, version } from "./client";
import type { Project, Schema } from "./types";
const id = encodeURIComponent;
export const api = {
  gitSettings: (pid: string) =>
    request<Schema["GitSettingsView"]>(`/projects/${id(pid)}/git`),
  saveGitSettings: (pid: string, body: Schema["GitSettingsInput"]) =>
    request<Schema["GitSettingsView"]>(`/projects/${id(pid)}/git`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  testGit: (pid: string, expected_version: string) =>
    request<Schema["GitSettingsView"]>(`/projects/${id(pid)}/git/test`, {
      method: "POST",
      signal: AbortSignal.timeout(75000),
      body: JSON.stringify({ expected_version }),
    }),
  pushPreview: (rid: string) =>
    request<Schema["PushView"]>(`/runs/${id(rid)}/push`),
  pushBranch: (rid: string, revision: number, body: Schema["PushInput"]) =>
    request<Schema["PushView"]>(`/runs/${id(rid)}/push`, {
      method: "POST",
      headers: version("run", rid, revision),
      signal: AbortSignal.timeout(75000),
      body: JSON.stringify(body),
    }),
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
  webhookSettings: () =>
    request<Schema["WebhookSettingsView"]>("/settings/webhooks"),
  saveWebhookSettings: (body: Schema["WebhookSettingsInput"]) =>
    request<Schema["WebhookSettingsView"]>("/settings/webhooks", {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  testWebhook: (expected_version: string) =>
    request<Schema["WebhookSettingsView"]>("/settings/webhooks/test", {
      method: "POST",
      body: JSON.stringify({ expected_version }),
    }),
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
  notifications: (before?: number) =>
    request<Schema["NotificationPage"]>(
      `/notifications${before ? `?before=${before}` : ""}`,
    ),
  readNotifications: (body: Schema["NotificationRead"]) =>
    request<void>("/notifications/read", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  dashboard: () => request<Schema["DashboardView"]>("/dashboard"),
  validationProfiles: () =>
    request<Schema["ValidationCandidate"][]>("/validation-profiles"),
  detectValidation: (body: Schema["ValidationDetectInput"]) =>
    request<Schema["ValidationDetection"]>("/project-validation/detect", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  directories: (path?: string) =>
    request<Schema["DirectoryView"]>("/project-directories", {
      method: "POST",
      body: JSON.stringify({ path: path || null }),
    }),
  inspectRepository: (path: string) =>
    request<Schema["RepositoryPreview"]>("/project-repository/inspect", {
      method: "POST",
      body: JSON.stringify({ path }),
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
  testChanges: (rid: string) =>
    request<Schema["TestChangeView"]>(`/runs/${id(rid)}/test-changes`),
  steps: (rid: string) => request<Schema["StepPage"]>(`/runs/${id(rid)}/steps`),
  report: (rid: string) =>
    request<Schema["ReportView"]>(`/runs/${id(rid)}/report`),
  spec: (rid: string) =>
    request<Schema["SpecContent"]>(`/runs/${id(rid)}/spec`),
  logWindow: (rid: string, before?: number, stepId?: number) => {
    const params = new URLSearchParams();
    if (before !== undefined) params.set("before", String(before));
    if (stepId !== undefined) params.set("step_id", String(stepId));
    return request<Schema["LogWindow"]>(
      `/runs/${id(rid)}/logs/window?${params}`,
    );
  },
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
      | "approve-plan"
      | "revise-plan"
      | "approve-test-changes"
      | "reject-test-changes"
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
