import { ProjectDetailPage } from "../src/pages/ProjectDetailPage";
import { ProjectsPage } from "../src/pages/ProjectsPage";
import { useState } from "react";
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
import { RunDraftProvider } from "../src/hooks/runDraft";
import { DashboardPage } from "../src/pages/DashboardPage";
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
  name: "r1",
  worktree_removed: false,
  push_status: "not_pushed",
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

it("재진입 시 정리 실패를 표시하고 Push·정리 재시도·상태 조정을 같은 줄에 둔다", async () => {
  setupDetail();
  vi.mocked(api.run).mockResolvedValue({
    ...detail,
    state: "completed",
    execution: {
      worker_liveness: "stopped",
      cleanup: "confirmed",
      worktree_removed: false,
      cleanup_status: "needs_input",
      cleanup_error: "build/personal.txt 파일을 보존합니다.",
    },
    handoff: {
      repository: "/tmp/repo",
      branch: "feature/run",
      status: "matching",
      commands: [],
      notice: "",
    },
  });
  vi.spyOn(api, "pushPreview").mockResolvedValue({
    remote: "origin",
    remote_url: "https://example.invalid/repo.git",
    settings_version: "v1",
    branch: "feature/run",
    approved_sha: "a".repeat(40),
    result: null,
  });
  vi.spyOn(api, "gitSettings").mockResolvedValue({
    remote: "origin",
    remote_url: "https://example.invalid/repo.git",
    account: "",
    token_registered: false,
    version: "v1",
    remotes: [],
    test: {
      status: "success",
      message: "조회 완료",
      url: "https://example.invalid/repo.git",
      checked_at: "2026-11-12",
      push_permission: "unknown",
    },
  });
  const request = vi.spyOn(api, "pushBranch");
  wrap(
    <Routes>
      <Route path="/runs/:id" element={<RunDetailPage />} />
    </Routes>,
    "/runs/r1",
  );
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "build/personal.txt 파일을 보존합니다.",
  );
  const push = await screen.findByRole("button", { name: "작업 브랜치 Push" });
  const row = push.closest(".inline-actions");
  expect(row).toContainElement(
    screen.getByRole("button", { name: "Worktree 정리 재시도" }),
  );
  expect(row).toContainElement(
    screen.getByRole("button", { name: "상태 조정" }),
  );
  expect(request).not.toHaveBeenCalled();
});

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
    const notice = await screen.findByText(/계속하려면 새 작업을/);
    expect(notice).toBeVisible();
    expect(notice.closest(".run-actions")).toBeNull();
    expect(notice.closest('[role="status"]')).toHaveClass("worktree-notice");
    expect(screen.getByText("Worktree 정리 완료")).toBeVisible();
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
    vi.spyOn(api, "agentSettings").mockResolvedValue({
      version: "v",
      settings: { model: "task-model", roles: {} },
    });
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
    await userEvent.click(screen.getByText("브랜치·추가 지시사항 (선택)"));
    expect(
      await screen.findByText("custom/tasks/add-task-search"),
    ).toBeVisible();
    expect(
      screen.getByLabelText("Feature 브랜치 (선택)"),
    ).toHaveAccessibleDescription(/custom\/tasks\//);
    await userEvent.type(screen.getByLabelText("PRD"), "작업 요구사항");
    expect(screen.getByLabelText("작업명")).toBeRequired();
    await userEvent.type(
      screen.getByLabelText("작업명"),
      "  로그인 화면 개선  ",
    );
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
    await userEvent.click(screen.getByText("모델·자동 수정 설정 (선택)"));
    await userEvent.click(
      screen.getByRole("button", { name: "이 작업만 변경" }),
    );
    await screen.findByLabelText("공통 기본값 모델");
    await userEvent.click(screen.getByRole("button", { name: "실행 전 확인" }));
    expect(
      await screen.findByRole("button", { name: "확인하고 작업 시작" }),
    ).toBeVisible();
    expect(api.preview).toHaveBeenCalledWith(
      expect.objectContaining({
        test_scope: "affected",
        name: "로그인 화면 개선",
        agent_settings: { model: "task-model", roles: {} },
      }),
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
  ).toHaveAttribute("title", new Date(created_at).toLocaleString("ko-KR"));
  expect(
    document.querySelector(`time[datetime="${completed_at}"]`),
  ).toHaveAttribute("title", new Date(completed_at).toLocaleString("ko-KR"));
  expect(screen.getByRole("row", { name: /pending/ })).toHaveTextContent("—");
});

it("작업 목록에 작업명과 ID 및 Push·Worktree 상태를 표시한다", () => {
  wrap(
    <RunsTable
      runs={[
        {
          ...detail,
          name: "로그인 개선",
          push_status: "success",
          worktree_removed: true,
        },
        {
          ...detail,
          id: "legacy",
          push_status: "not_pushed",
          worktree_removed: false,
        },
      ]}
    />,
  );
  const named = screen.getByRole("row", { name: /로그인 개선/ });
  expect(named).toHaveTextContent("r1");
  expect(named).toHaveTextContent("성공");
  expect(named).toHaveTextContent("정리 완료");
  const legacy = screen.getByRole("row", { name: /legacy/ });
  expect(legacy).toHaveTextContent("미실행");
  expect(legacy).toHaveTextContent("미정리");
});

it("작업 상세 제목에 작업명을 표시하고 ID도 유지한다", async () => {
  setupDetail();
  vi.mocked(api.run).mockResolvedValue({ ...detail, name: "로그인 개선" });
  wrap(
    <Routes>
      <Route path="/runs/:id" element={<RunDetailPage />} />
    </Routes>,
    "/runs/r1",
  );
  expect(
    await screen.findByRole("heading", { name: "로그인 개선" }),
  ).toBeVisible();
  expect(screen.getByText("작업 ID: r1")).toBeVisible();
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

it("새 작업의 많은 검증 명령을 요약하고 펼쳐서 확인한다", async () => {
  const commands = Object.fromEntries(
    Array.from({ length: 30 }, (_, i) => [
      `check-${i}`,
      {
        kind: i === 0 ? ("test" as const) : ("check" as const),
        argv: ["tool", `check-${i}`],
        timeout_seconds: 600,
        working_directory: ".",
        network: "none" as const,
        junit_report: i === 0 ? "report.xml" : null,
      },
    ]),
  );
  vi.spyOn(api, "projects").mockResolvedValue({
    items: [p],
    has_more: false,
    next_cursor: null,
  });
  vi.spyOn(api, "project").mockResolvedValue({
    ...p,
    policy: { ...p.policy, tests: { ...p.policy.tests, commands } },
  });
  vi.spyOn(api, "branches").mockResolvedValue({ items: ["main"] });
  wrap(<NewRunPage />, "/runs/new?project=prj-1");
  const summary = await screen.findByText("검증 명령 30개 자세히 보기");
  expect(screen.getByText("테스트 1개 · 빌드·정적 검사 29개")).toBeVisible();
  expect(screen.getByText("tool check-29")).not.toBeVisible();
  await userEvent.click(summary);
  expect(screen.getByText("tool check-29")).toBeVisible();
  expect(
    screen.getByRole("region", { name: "검증 명령 목록" }),
  ).toHaveAttribute("tabindex", "0");
  await userEvent.click(summary);
  expect(screen.getByText("tool check-29")).not.toBeVisible();
});

it("초안을 유지한 채 검증 설정에서 돌아오고 로그아웃 경계에서 폐기한다", async () => {
  vi.spyOn(api, "projects").mockResolvedValue({
    items: [p],
    has_more: false,
    next_cursor: null,
  });
  vi.spyOn(api, "project").mockResolvedValue(p);
  vi.spyOn(api, "branches").mockResolvedValue({ items: ["main"] });
  function Fixture() {
    const [visible, setVisible] = useState(true);
    return (
      <>
        <button onClick={() => setVisible(!visible)}>화면 이동</button>
        {visible && <NewRunPage />}
      </>
    );
  }
  const view = wrap(
    <RunDraftProvider>
      <Fixture />
    </RunDraftProvider>,
    "/runs/new?project=prj-1",
  );
  await userEvent.type(screen.getByLabelText("작업명"), "보존할 작업");
  await userEvent.type(screen.getByLabelText("PRD"), "보존할 요구사항");
  await userEvent.click(screen.getByText("화면 이동"));
  await userEvent.click(screen.getByText("화면 이동"));
  expect(screen.getByLabelText("작업명")).toHaveValue("보존할 작업");
  expect(screen.getByLabelText("PRD")).toHaveValue("보존할 요구사항");
  view.unmount();
  wrap(
    <RunDraftProvider>
      <NewRunPage />
    </RunDraftProvider>,
  );
  expect(screen.getByLabelText("PRD")).toHaveValue("");
});
it("프로젝트가 없으면 등록 경로와 실행 불가 이유를 안내한다", async () => {
  vi.spyOn(api, "projects").mockResolvedValue({
    items: [],
    has_more: false,
    next_cursor: null,
  });
  wrap(<NewRunPage />);
  expect(
    await screen.findByText("먼저 프로젝트를 등록해 주세요"),
  ).toBeVisible();
  expect(
    screen.getByRole("link", { name: "프로젝트 등록하러 가기" }),
  ).toHaveAttribute("href", "/projects");
  expect(screen.getByRole("button", { name: "실행 전 확인" })).toBeDisabled();
});
it("승인 대기 결과를 인계 정보보다 먼저 표시하고 테스트 탭을 기본 선택한다", async () => {
  setupDetail();
  wrap(
    <Routes>
      <Route path="/runs/:id" element={<RunDetailPage />} />
    </Routes>,
    "/runs/r1",
  );
  const tab = await screen.findByRole("tab", { name: "테스트" });
  expect(tab).toHaveAttribute("aria-selected", "true");
  expect(
    document
      .querySelector(".results-panel")!
      .compareDocumentPosition(document.querySelector(".run-execution")!) &
      Node.DOCUMENT_POSITION_FOLLOWING,
  ).toBeTruthy();
});
it("승인 요약 카드의 묶음 필터가 두 상태의 다음 페이지까지 조회한다", async () => {
  vi.spyOn(api, "dashboard").mockResolvedValue({
    project_count: 1,
    counts_by_state: {},
  } as Schema["DashboardView"]);
  const get = vi.spyOn(api, "runs").mockImplementation(async (params) => ({
    items: [
      {
        ...detail,
        id: params.get("state")! + (params.get("cursor") ?? ""),
        name: params.get("state")! + (params.get("cursor") ?? ""),
      },
    ],
    has_more: !params.get("cursor"),
    next_cursor: params.get("cursor") ? null : "next",
  }));
  wrap(<DashboardPage />, "/?state=approvals");
  expect(
    await screen.findByRole("link", {
      name: "waiting_plan_approvalnext",
    }),
  ).toBeVisible();
  expect(get).toHaveBeenCalledTimes(4);
  expect(screen.getByRole("link", { name: /등록 프로젝트/ })).toHaveAttribute(
    "href",
    "/projects",
  );
});

it("프로젝트 설정 탭을 이동해도 편집 중인 정책을 유지한다", async () => {
  vi.spyOn(api, "project").mockResolvedValue(p);
  vi.spyOn(api, "gitSettings").mockResolvedValue({
    remote: "",
    remote_url: null,
    account: "",
    token_registered: false,
    version: "1",
    remotes: [],
    test: null,
  });
  wrap(
    <Routes>
      <Route path="/projects/:id" element={<ProjectDetailPage />} />
    </Routes>,
    "/projects/prj-1",
  );
  const input = await screen.findByLabelText("프로젝트 이름");
  await userEvent.clear(input);
  await userEvent.type(input, "수정 중 이름");
  await userEvent.click(screen.getByRole("tab", { name: "Git 계정·인증" }));
  expect(input).not.toBeVisible();
  await userEvent.click(
    screen.getByRole("tab", { name: "기본 정보·실행 정책" }),
  );
  expect(input).toBeVisible();
  expect(input).toHaveValue("수정 중 이름");
});
it("등록 대화상자의 하단 버튼으로 연결된 프로젝트 폼을 제출한다", async () => {
  vi.spyOn(api, "projects").mockResolvedValue({
    items: [],
    has_more: false,
    next_cursor: null,
  });
  const create = vi.spyOn(api, "createProject").mockResolvedValue(p);
  wrap(<ProjectsPage />);
  await userEvent.click(
    await screen.findByRole("button", { name: "프로젝트 등록" }),
  );
  await userEvent.type(screen.getByLabelText("프로젝트 이름"), "등록 예제");
  await userEvent.type(
    screen.getByLabelText("저장소 절대 경로"),
    "/projects/example",
  );
  await userEvent.click(screen.getByText("고급 설정 · 정책 JSON 직접 편집"));
  fireEvent.change(screen.getByLabelText("테스트·리뷰 정책 (JSON)"), {
    target: { value: JSON.stringify(initialPolicy) },
  });
  await userEvent.click(
    screen.getAllByRole("button", { name: "프로젝트 등록" }).at(-1)!,
  );
  await waitFor(() =>
    expect(create).toHaveBeenCalledWith(
      expect.objectContaining({
        name: "등록 예제",
        repository_path: "/projects/example",
      }),
      expect.anything(),
    ),
  );
});

it("작업명 아래 프로젝트명과 브랜치를 표시하고 미등록 작업을 구분한다", () => {
  wrap(
    <RunsTable
      runs={[
        {
          ...detail,
          id: "project-run",
          name: "같은 작업명",
          project_name: "웹 프로젝트",
          branch: "feature/long-branch",
        },
        {
          ...detail,
          id: "cli-run",
          name: "같은 작업명",
          project_name: null,
          branch: null,
        },
      ]}
    />,
  );
  const project = screen.getByText("웹 프로젝트");
  expect(project.closest("td")).toHaveTextContent("같은 작업명");
  expect(project.closest(".run-metadata")).toHaveTextContent(
    "feature/long-branch",
  );
  expect(project).toHaveAttribute("title", "웹 프로젝트");
  expect(screen.getByText("feature/long-branch")).toHaveAttribute(
    "title",
    "feature/long-branch",
  );
  expect(screen.getByText("미등록 작업")).toBeVisible();
  expect(screen.getByText("브랜치 준비 중")).toBeVisible();
});

it.each([
  {
    project_name: "예제 프로젝트",
    project_id: "prj-1",
    expected: "예제 프로젝트",
  },
  { project_name: null, project_id: null, expected: "미등록 작업" },
  { project_name: null, project_id: "prj-1", expected: "프로젝트명 확인 불가" },
])(
  "작업 상세에 프로젝트 정보를 표시한다: $expected",
  async ({ expected, ...project }) => {
    setupDetail();
    vi.mocked(api.run).mockResolvedValue({ ...detail, ...project });
    wrap(
      <Routes>
        <Route path="/runs/:id" element={<RunDetailPage />} />
      </Routes>,
      "/runs/r1",
    );
    const name = await screen.findByText(expected);
    expect(name).toBeVisible();
    expect(name.closest(".run-identity")).toHaveTextContent("프로젝트:");
  },
);

it("새 작업에서 Git 미설정 상태와 설정 링크를 안내해도 작업 입력을 허용한다", async () => {
  vi.spyOn(api, "projects").mockResolvedValue({
    items: [p],
    has_more: false,
    next_cursor: null,
  });
  vi.spyOn(api, "project").mockResolvedValue(p);
  vi.spyOn(api, "branches").mockResolvedValue({ items: ["main"] });
  vi.spyOn(api, "gitSettings").mockResolvedValue({
    remote: "",
    remote_url: null,
    account: "",
    token_registered: false,
    version: "v1",
    remotes: [],
    test: null,
  });
  wrap(<NewRunPage />, "/runs/new?project=prj-1");
  expect(
    await screen.findByText("프로젝트에서 Push 원격 저장소를 설정하세요."),
  ).toBeVisible();
  expect(
    screen.getByRole("link", { name: "Git 계정·인증 설정" }),
  ).toHaveAttribute("href", "/projects/prj-1?tab=git");
  expect(screen.getByLabelText("작업명")).toBeEnabled();
  expect(screen.queryByRole("button", { name: "작업 브랜치 Push" })).toBeNull();
});

it("Git 설정 링크로 진입하면 인증 탭을 바로 표시한다", async () => {
  vi.spyOn(api, "project").mockResolvedValue(p);
  vi.spyOn(api, "gitSettings").mockResolvedValue({
    remote: "",
    remote_url: null,
    account: "",
    token_registered: false,
    version: "v1",
    remotes: [],
    test: null,
  });
  wrap(
    <Routes>
      <Route path="/projects/:id" element={<ProjectDetailPage />} />
    </Routes>,
    "/projects/prj-1?tab=git",
  );
  expect(
    await screen.findByRole("heading", { name: "Git 계정 및 인증" }),
  ).toBeVisible();
  expect(screen.getByRole("tab", { name: "Git 계정·인증" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
});

it("새 작업의 프로젝트를 바꿔도 이전 연결 테스트 결과가 새 프로젝트에 섞이지 않는다", async () => {
  const client = createQueryClient();
  const second = { ...p, id: "prj-2", name: "다른 프로젝트" };
  const connection = {
    remote: "origin",
    remote_url: "https://example.invalid/first.git",
    account: "",
    token_registered: false,
    version: "v1",
    remotes: [],
    test: null,
  };
  vi.spyOn(api, "projects").mockResolvedValue({
    items: [p, second],
    has_more: false,
    next_cursor: null,
  });
  vi.spyOn(api, "project").mockImplementation(async (id) =>
    id === p.id ? p : second,
  );
  vi.spyOn(api, "branches").mockResolvedValue({ items: ["main"] });
  vi.spyOn(api, "gitSettings").mockImplementation(async (id) => ({
    ...connection,
    remote_url:
      id === p.id
        ? connection.remote_url
        : "https://example.invalid/second.git",
  }));
  let resolve!: (value: Schema["GitSettingsView"]) => void;
  vi.spyOn(api, "testGit").mockImplementation(
    () =>
      new Promise((r) => {
        resolve = r;
      }),
  );
  wrap(<NewRunPage />, "/runs/new", client);
  await screen.findByRole("option", { name: "다른 프로젝트" });
  fireEvent.change(screen.getByLabelText("프로젝트"), {
    target: { value: p.id },
  });
  await waitFor(() =>
    expect(
      screen.getByRole("button", { name: "Git 연결 테스트" }),
    ).toBeEnabled(),
  );
  fireEvent.click(screen.getByRole("button", { name: "Git 연결 테스트" }));
  await waitFor(() => expect(api.testGit).toHaveBeenCalled());
  fireEvent.change(screen.getByLabelText("프로젝트"), {
    target: { value: second.id },
  });
  await waitFor(() => expect(api.gitSettings).toHaveBeenCalledWith(second.id));
  await act(async () =>
    resolve({
      ...connection,
      test: {
        status: "success",
        message: "조회 완료",
        url: connection.remote_url,
        checked_at: "2026-11-12",
        push_permission: "unknown",
      },
    }),
  );
  expect(
    client.getQueryData<Schema["GitSettingsView"]>(["project-git", second.id])
      ?.remote_url,
  ).toBe("https://example.invalid/second.git");
  expect(
    client.getQueryData<Schema["GitSettingsView"]>(["project-git", p.id])?.test
      ?.status,
  ).toBe("success");
});
