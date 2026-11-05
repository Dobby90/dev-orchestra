import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { App, createQueryClient } from "../src/App";
import { LoginPage } from "../src/pages/LoginPage";
import { NewRunPage } from "../src/pages/NewRunPage";
import { RunDetailPage } from "../src/pages/RunDetailPage";
import { Routes, Route } from "react-router-dom";
import { RunsTable, Status, states } from "../src/components/common";
import { api } from "../src/api/resources";
import type { Project, Schema, State } from "../src/api/types";
import { ProjectForm, initialPolicy } from "../src/components/ProjectForm";
const p: Project = {
  id: "prj-1",
  name: "프로젝트",
  repository_path: "/tmp/repo",
  default_base_branch: "main",
  branch_prefix: "custom/tasks",
  version: 0,
  created_at: "2026-11-02",
  updated_at: "2026-11-02",
  archived_at: null,
  policy: initialPolicy as Project["policy"],
  schema_version: 1,
};
const detail: Schema["RunDetail"] = {
  test_scope: "full",
  deletion_pending: false,
  test_change_pending: false,
  test_change_count: 0,
  schema_version: 1,
  revision: 0,
  validation: { status: "valid", revision: 0 },
  id: "r1",
  state: "waiting_approval",
  version: 4,
  current_sha: "a".repeat(40),
  branch: "feature/run",
  base_sha: "b".repeat(40),
  final_sha: null,
  iteration: 0,
  current_role: null,
  control: null,
  event_cursor: 0,
  project_id: "prj-1",
  base_branch: "main",
  created_at: "2026-11-02T01:00:00Z",
  max_fix_attempts: 3,
  questions: [],
  error: null,
};
function wrap(node: React.ReactNode, path = "/", client = createQueryClient()) {
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>{node}</MemoryRouter>
    </QueryClientProvider>,
  );
}
class Stream {
  onopen = null;
  onerror = null;
  addEventListener() {}
  close() {}
}
const operation: Schema["OperationView"] = {
  id: "op-1",
  run_id: "r1",
  kind: "approve",
  status: "applied",
  state: "waiting_approval",
  result: {},
  created_at: "2026-11-02",
  applied_at: "2026-11-02",
  error: null,
};
const emptyTests: Schema["TestResultView"] = {
  status: "not_run",
  step_id: null,
  sha: null,
  revision: null,
  duration_seconds: null,
  total: null,
  passed: null,
  failed: null,
  skipped: null,
  commands: [],
  failures: [],
  history: [],
  legacy_incomplete: true,
};
function setupDetail() {
  vi.spyOn(api, "operation").mockResolvedValue(operation);
  vi.spyOn(api, "tests").mockResolvedValue(emptyTests);
  vi.stubGlobal("EventSource", Stream);
  vi.spyOn(api, "run").mockResolvedValue(detail);
  vi.spyOn(api, "steps").mockResolvedValue({ items: [] });
}

it.each(["failed", "needs_input"] as const)(
  "%s 작업의 정리 결과가 반영되면 재개 버튼을 숨기고 새 작업 생성을 안내한다",
  async (state) => {
    setupDetail();
    const run = {
      ...detail,
      state,
      allowed_actions: ["resume", "cleanup"],
      execution: {
        worker_liveness: "stopped" as const,
        cleanup: "confirmed" as const,
        worktree_removed: false,
      },
    };
    vi.mocked(api.run).mockResolvedValue(run);
    const client = createQueryClient();
    wrap(
      <Routes>
        <Route path="/runs/:id" element={<RunDetailPage />} />
      </Routes>,
      "/runs/r1",
      client,
    );
    expect(await screen.findByRole("button", { name: "재개" })).toBeEnabled();
    vi.mocked(api.run).mockResolvedValue({
      ...run,
      version: 5,
      allowed_actions: ["reconcile"],
      execution: { ...run.execution, worktree_removed: true },
    });
    await act(async () => {
      await client.invalidateQueries({ queryKey: ["run", "r1"] });
    });
    expect(await screen.findByText(/계속하려면 새 작업을/)).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "재개" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Worktree 정리" }),
    ).not.toBeInTheDocument();
  },
);

describe("화면·위험 명령", () => {
  it("초기 401 이후 로딩을 끝내고 원래 URL에서 로그인 화면을 표시한다", async () => {
    window.history.replaceState({}, "", "/runs/saved");
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          new Response('{"code":"SESSION_REQUIRED"}', { status: 401 }),
        ),
    );
    render(<App />);
    expect(await screen.findByLabelText("일회용 로그인 코드")).toBeVisible();
    expect(window.location.pathname).toBe("/runs/saved");
    window.history.replaceState({}, "", "/");
  });
  it("세션 query를 교체하지 않고 로그인 후 대시보드를 표시한다", async () => {
    window.history.replaceState({}, "", "/");
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input, init) => {
        const url = String(input);
        if (url.endsWith("/session") && init?.method === "POST")
          return new Response(
            JSON.stringify({
              authenticated: true,
              csrf_token: "csrf",
              expires_at: "2026-11-02T10:00:00Z",
            }),
          );
        if (url.endsWith("/session"))
          return new Response('{"code":"SESSION_REQUIRED"}', { status: 401 });
        if (url.endsWith("/dashboard"))
          return new Response('{"project_count":0,"counts_by_state":{}}');
        return new Response('{"items":[],"has_more":false,"next_cursor":null}');
      }),
    );
    render(<App />);
    await userEvent.type(
      await screen.findByLabelText("일회용 로그인 코드"),
      "s".repeat(43),
    );
    await userEvent.click(
      screen.getByRole("button", { name: "작업 공간 열기" }),
    );
    expect(
      await screen.findByRole("heading", { name: "작업 현황" }),
    ).toBeVisible();
  });
  it("14개 상태에 텍스트 라벨이 있다", () => {
    for (const state of Object.keys(states)) {
      const { unmount } = render(<Status state={state as State} />);
      expect(screen.getByText(states[state as State].label)).toBeVisible();
      unmount();
    }
  });
  it("일회용 코드를 URL·저장소에 남기지 않고 로그인한다", async () => {
    const code = "s".repeat(43);
    const login = vi.spyOn(api, "login").mockResolvedValue({
      authenticated: true,
      csrf_token: "csrf",
      expires_at: "2026-11-02T10:00:00Z",
    });
    const success = vi.fn();
    wrap(<LoginPage onSuccess={success} />);
    await userEvent.type(screen.getByLabelText("일회용 로그인 코드"), code);
    await userEvent.click(
      screen.getByRole("button", { name: "작업 공간 열기" }),
    );
    await waitFor(() => expect(success).toHaveBeenCalled());
    expect(login).toHaveBeenCalledWith(code, expect.anything());
    expect(localStorage.getItem("csrf")).toBeNull();
    expect(screen.getByLabelText("일회용 로그인 코드")).toHaveValue("");
  });
  it("SHA 전체 확인과 현재 버전으로만 승인 요청을 보낸다", async () => {
    setupDetail();
    const control = vi.spyOn(api, "control").mockResolvedValue(operation);
    wrap(
      <Routes>
        <Route path="/runs/:id" element={<RunDetailPage />} />
      </Routes>,
      "/runs/r1",
    );
    await userEvent.click(
      await screen.findByRole("button", { name: "최종 승인" }),
    );
    const confirm = screen.getByRole("button", { name: "최종 승인 확인" });
    expect(confirm).toBeDisabled();
    fireEvent.submit(confirm.closest("form")!);
    expect(control).not.toHaveBeenCalled();
    await userEvent.type(
      screen.getByLabelText("위 커밋 SHA 전체를 입력해 확인하세요"),
      "a".repeat(40),
    );
    await userEvent.click(confirm);
    await waitFor(() =>
      expect(control).toHaveBeenCalledWith(
        "r1",
        4,
        "approve",
        {
          expected_sha: "a".repeat(40),
        },
        expect.any(String),
      ),
    );
  });
  it("승인 대화상자를 연 뒤 version이 바뀌면 확인을 차단한다", async () => {
    setupDetail();
    const client = createQueryClient();
    wrap(
      <Routes>
        <Route path="/runs/:id" element={<RunDetailPage />} />
      </Routes>,
      "/runs/r1",
      client,
    );
    await userEvent.click(
      await screen.findByRole("button", { name: "최종 승인" }),
    );
    await userEvent.type(
      screen.getByLabelText("위 커밋 SHA 전체를 입력해 확인하세요"),
      "a".repeat(40),
    );
    vi.mocked(api.run).mockResolvedValue({ ...detail, version: 5 });
    await act(async () => {
      await client.invalidateQueries({ queryKey: ["run", "r1"] });
    });
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "최종 승인 확인" }),
      ).toBeDisabled(),
    );
    expect(screen.getByText(/작업 상태가 변경되었습니다/)).toBeVisible();
  });
  it("취소 접수 후 Worker 확정 전에 Cancelled로 표시하지 않는다", async () => {
    setupDetail();
    vi.mocked(api.run).mockResolvedValue({
      ...detail,
      state: "developing",
      current_role: "developer",
    });
    vi.spyOn(api, "control").mockImplementation(async () => {
      vi.mocked(api.run).mockResolvedValue({
        ...detail,
        state: "developing",
        control: "cancel",
        version: 5,
      });
      return operation;
    });
    wrap(
      <Routes>
        <Route path="/runs/:id" element={<RunDetailPage />} />
      </Routes>,
      "/runs/r1",
    );
    await userEvent.click(await screen.findByRole("button", { name: "취소" }));
    await userEvent.click(
      screen.getByRole("button", { name: "작업 취소 확인" }),
    );
    expect(await screen.findByText(/취소 요청이 접수되었습니다/)).toBeVisible();
    expect(screen.getByText("개발 중")).toBeVisible();
    expect(screen.queryByText("취소됨")).not.toBeInTheDocument();
  });
  it("CLI 미등록 작업은 조회 전용이며 증거 없는 결과를 통과로 표시하지 않는다", async () => {
    setupDetail();
    vi.mocked(api.run).mockResolvedValue({ ...detail, project_id: null });
    wrap(
      <Routes>
        <Route path="/runs/:id" element={<RunDetailPage />} />
      </Routes>,
      "/runs/r1?tab=tests",
    );
    expect(await screen.findByText(/CLI 작업 · 조회 전용/)).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "최종 승인" }),
    ).not.toBeInTheDocument();
    expect(await screen.findByText(/아직 실행되지 않음/)).toBeVisible();
  });
  it("보고서 HTML·스크립트·외부 이미지를 실행하지 않는다", async () => {
    setupDetail();
    vi.spyOn(api, "report").mockResolvedValue({
      stale: true,
      markdown:
        "# 결과\n<script>window.CANARY=true</script>\n![외부](https://external.invalid/x)\n[링크](javascript:alert(1))",
    });
    wrap(
      <Routes>
        <Route path="/runs/:id" element={<RunDetailPage />} />
      </Routes>,
      "/runs/r1?tab=report",
    );
    expect(await screen.findByRole("heading", { name: "결과" })).toBeVisible();
    expect(document.querySelector(".markdown script")).toBeNull();
    expect(document.querySelector(".markdown img")).toBeNull();
    expect(document.querySelector(".markdown a")).toBeNull();
  });
  it("정책 가이드를 열고 Escape로 닫아도 폼을 제출하지 않는다", async () => {
    const onSubmit = vi.fn();
    wrap(<ProjectForm pending={false} onSubmit={onSubmit} />);
    await userEvent.click(
      screen.getByRole("button", { name: "정책 JSON 작성 가이드" }),
    );
    expect(
      screen.getByRole("dialog", { name: "프로젝트 정책 JSON 작성 가이드" }),
    ).toBeVisible();
    expect(screen.getByText(/critical·high는 반드시 포함/)).toBeVisible();
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(onSubmit).not.toHaveBeenCalled();
    expect(
      screen.getByRole("button", { name: "정책 JSON 작성 가이드" }),
    ).toHaveFocus();
  });
  it("변경 영향 범위를 기본 선택하고 레이어 미리보기를 닫은 뒤 입력을 변경하면 무효화한다", async () => {
    vi.spyOn(api, "projects").mockResolvedValue({
      items: [p],
      has_more: false,
      next_cursor: null,
    });
    vi.spyOn(api, "project").mockResolvedValue(p);
    vi.spyOn(api, "branches").mockResolvedValue({ items: ["main"] });
    vi.spyOn(api, "preview").mockResolvedValue({
      test_scope: "full",
      test_scope_notice: "영향 판단 실패 시 전체 검사",
      run_id: "r1",
      branch: "feature/x",
      base_sha: "b".repeat(40),
      project_version: 0,
      agent_settings_hash: "settings",
      proposal_hash: "h",
      expires: Date.now() / 1000 + 300,
      preview_token: "receipt",
      spec_sha256: "hash",
    });
    wrap(<NewRunPage />, "/runs/new?project=prj-1");
    expect(
      await screen.findByText("custom/tasks/add-task-search"),
    ).toBeVisible();
    expect(
      screen.getByLabelText("Feature 브랜치 (선택)"),
    ).toHaveAccessibleDescription(/custom\/tasks\//);
    await userEvent.type(screen.getByLabelText("PRD"), "작업 요구사항");
    await userEvent.click(
      screen.getByRole("button", { name: "PRD 작성 가이드" }),
    );
    expect(
      screen.getByRole("dialog", { name: "PRD 작성 가이드" }),
    ).toBeVisible();
    expect(screen.getByText(/한 줄로도 입력할 수 있습니다/)).toBeVisible();
    await userEvent.keyboard("{Escape}");
    expect(screen.getByLabelText("PRD")).toHaveValue("작업 요구사항");
    expect(api.preview).not.toHaveBeenCalled();
    expect(screen.getByLabelText("테스트 범위")).toHaveValue("affected");
    await userEvent.click(screen.getByRole("button", { name: "실행 전 확인" }));
    expect(
      await screen.findByRole("button", { name: "확인하고 작업 시작" }),
    ).toBeVisible();
    expect(api.preview).toHaveBeenCalledWith(
      expect.objectContaining({ test_scope: "affected" }),
    );
    expect(screen.getByRole("dialog", { name: "실행 미리보기" })).toBeVisible();
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByLabelText("PRD")).toHaveValue("작업 요구사항");
    await userEvent.selectOptions(screen.getByLabelText("테스트 범위"), "full");
    expect(
      screen.queryByRole("button", { name: "확인하고 작업 시작" }),
    ).not.toBeInTheDocument();
  });
});

it("기획안과 인수 조건을 확인하고 해당 기획 버전으로 개발을 승인한다", async () => {
  setupDetail();
  vi.mocked(api.run).mockResolvedValue({
    ...detail,
    state: "waiting_plan_approval",
    plan: "예외 처리 구현 계획",
    criteria: [{ id: "AC1", description: "실패 시 오류 표시" }],
    plan_hash: "c".repeat(64),
  });
  const control = vi.spyOn(api, "control").mockResolvedValue(operation);
  wrap(
    <Routes>
      <Route path="/runs/:id" element={<RunDetailPage />} />
    </Routes>,
    "/runs/r1",
  );
  expect(await screen.findByText("예외 처리 구현 계획")).toBeVisible();
  expect(screen.getByText("실패 시 오류 표시")).toBeVisible();
  await userEvent.click(
    screen.getByRole("button", { name: "기획 승인 후 개발 시작" }),
  );
  await userEvent.click(
    screen.getByRole("button", { name: "기획 승인 후 개발 시작 확인" }),
  );
  await waitFor(() =>
    expect(control).toHaveBeenCalledWith(
      "r1",
      4,
      "approve-plan",
      { plan_hash: "c".repeat(64), reason: "" },
      expect.any(String),
    ),
  );
});

it("작업 목록에 요청일시와 완료일시 및 미기록 표시를 제공한다", () => {
  const created_at = "2026-11-04T10:00:00Z";
  const completed_at = "2026-11-05T11:00:00Z";
  wrap(
    <RunsTable
      runs={[
        {
          ...detail,
          id: "finished",
          state: "completed",
          created_at,
          completed_at,
        },
        { ...detail, id: "pending", created_at: null, completed_at: null },
      ]}
    />,
  );
  expect(screen.getByRole("columnheader", { name: "요청일시" })).toBeVisible();
  expect(screen.getByRole("columnheader", { name: "완료일시" })).toBeVisible();
  expect(
    document.querySelector(`time[datetime="${created_at}"]`),
  ).toHaveTextContent(new Date(created_at).toLocaleString("ko-KR"));
  expect(
    document.querySelector(`time[datetime="${completed_at}"]`),
  ).toHaveTextContent(new Date(completed_at).toLocaleString("ko-KR"));
  expect(screen.getByRole("row", { name: /pending/ })).toHaveTextContent("—");
});

it("작업 상세 상단에서 긴 Feature 브랜치를 별도 행에 표시한다", async () => {
  setupDetail();
  const branch = `feature/${"long-task-name-".repeat(20)}`;
  vi.mocked(api.run).mockResolvedValue({ ...detail, branch });
  wrap(
    <Routes>
      <Route path="/runs/:id" element={<RunDetailPage />} />
    </Routes>,
    "/runs/r1",
  );
  const value = await screen.findByText(branch);
  expect(value.closest(".run-branch")).not.toBeNull();
  expect(value).toBeVisible();
  expect(screen.getByText("기준 브랜치").closest(".run-branch")).not.toBeNull();
});
