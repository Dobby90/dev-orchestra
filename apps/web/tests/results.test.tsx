import { fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { expect, it, vi } from "vitest";
import { api } from "../src/api/resources";
import { createQueryClient } from "../src/App";
import {
  TestsPanel,
  ReviewPanel,
  ChangesPanel,
} from "../src/components/ResultPanels";
import type { RunDetail, Schema } from "../src/api/types";
import { RunDetailPage } from "../src/pages/RunDetailPage";
function wrap(node: React.ReactNode) {
  return render(
    <QueryClientProvider client={createQueryClient()}>
      {node}
    </QueryClientProvider>,
  );
}
it("최종 보고서에서 상세 테스트를 필요할 때 열고 요약본을 내려받을 수 있다", async () => {
  const run: RunDetail = {
    id: "run-report",
    name: "보고서 확인",
    state: "completed",
    version: 2,
    revision: 0,
    iteration: 0,
    event_cursor: 0,
    questions: [],
    schema_version: 1,
    worktree_removed: false,
    push_status: "not_pushed",
    test_scope: "full",
    auto_approve_plan: false,
    deletion_pending: false,
    test_change_pending: false,
    test_change_count: 0,
    created_at: null,
    project_id: null,
    base_branch: null,
    max_fix_attempts: null,
    error: null,
  };
  vi.spyOn(api, "run").mockResolvedValue(run);
  vi.spyOn(api, "steps").mockResolvedValue({ items: [] });
  vi.spyOn(api, "logWindow").mockResolvedValue({
    items: [],
    has_more: false,
    latest_cursor: 0,
    before_cursor: 0,
  });
  vi.spyOn(api, "report").mockResolvedValue({
    markdown: "## 변경 요약\n\n## 검증 결과\n\n## 인계\n",
    artifact_id: "artifact-report",
    generated_version: 2,
    current_version: 2,
    stale: false,
  });
  const tests = vi.spyOn(api, "tests").mockResolvedValue({
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
    legacy_incomplete: false,
  });
  wrap(
    <MemoryRouter initialEntries={[`/runs/${run.id}?tab=report`]}>
      <Routes>
        <Route path="/runs/:id" element={<RunDetailPage />} />
      </Routes>
    </MemoryRouter>,
  );

  expect(
    await screen.findByRole("heading", { name: "검증 결과" }),
  ).toBeVisible();
  const evidence = within(
    screen.getByRole("navigation", { name: "보고서 상세 근거" }),
  );
  expect(
    evidence.getByRole("link", { name: "보존 보고서 저장" }),
  ).toHaveAttribute("href", "/api/runs/run-report/artifacts/artifact-report");
  expect(
    evidence.getByRole("link", { name: "코드 리뷰 보기" }),
  ).toHaveAttribute("href", "/runs/run-report?tab=review");
  expect(tests).not.toHaveBeenCalled();
  fireEvent.click(evidence.getByRole("link", { name: "테스트 보기" }));
  expect(
    await screen.findByRole("heading", { name: "아직 실행되지 않음" }),
  ).toBeVisible();
  expect(screen.getByRole("tab", { name: "테스트" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
});

it("JUnit 상세와 이전 revision 표시를 현재 통과와 구분한다", async () => {
  const data: Schema["TestResultView"] = {
    status: "failed",
    step_id: 1,
    sha: "a".repeat(40),
    revision: 0,
    duration_seconds: 1.25,
    total: 1,
    passed: 0,
    failed: 1,
    skipped: 0,
    commands: [
      {
        command_id: "unit",
        status: "failed",
        duration_seconds: 1.25,
        exit_code: 1,
        argv: ["pytest"],
        junit_artifact_id: "junit",
        failure_details: [
          {
            name: "broken",
            message: "mismatch",
            details: "<script>CANARY</script>",
          },
        ],
      },
      {
        command_id: "build",
        status: "timeout",
        argv: ["npm", "run", "build"],
        error: "빌드 제한 시간 초과",
      },
    ],
    failures: [],
    history: [],
    legacy_incomplete: false,
  };
  vi.spyOn(api, "tests").mockResolvedValue(data);
  wrap(<TestsPanel rid="r" version={2} revision={1} />);
  expect(await screen.findByText(/이전 revision 0의 결과입니다/)).toBeVisible();
  expect(screen.getByRole("heading", { name: "테스트 실패" })).toBeVisible();
  const summary = within(screen.getByRole("region", { name: "실패 요약" }));
  expect(summary.getByText(/broken · mismatch/)).toBeVisible();
  expect(summary.getByText(/시간 초과 · 빌드 제한 시간 초과/)).toBeVisible();
  expect(screen.getByText("pytest")).not.toBeVisible();
  expect(screen.getByText("보존된 JUnit")).not.toBeVisible();
  fireEvent.click(screen.getByText("실행 세부 정보"));
  expect(screen.getByText("pytest")).toBeVisible();
  expect(screen.getByRole("link", { name: "보존된 JUnit" })).toHaveAttribute(
    "href",
    "/api/runs/r/artifacts/junit",
  );
  const raw = screen.getByText("<script>CANARY</script>");
  expect(raw).not.toBeVisible();
  fireEvent.click(screen.getByText("broken · mismatch", { exact: true }));
  expect(raw).toBeVisible();
  expect(document.querySelector("script")).toBeNull();
});
it("미해결 차단 리뷰를 resolved로 표시하거나 사용자 제외로 우회하지 않는다", async () => {
  vi.spyOn(api, "reviews").mockResolvedValue({
    status: "available",
    step_id: 1,
    sha: "a".repeat(40),
    revision: 0,
    summary: "review",
    issues: [
      {
        id: "model1",
        issue_id: "issue-host",
        severity: "high",
        file: "a.py",
        line: 1,
        description: "경합",
        evidence: "근거",
        suggestion: "잠금",
        status: "unresolved",
        source_sha: "a".repeat(40),
        resolution_evidence: null,
      },
    ],
    history: [],
    legacy_incomplete: false,
    blocking_severities: ["critical", "high"],
  });
  wrap(<ReviewPanel rid="r" version={2} revision={0} canClassify />);
  expect(await screen.findByText("미해결")).toBeVisible();
  expect(screen.queryByText("독립 재검증으로 해결")).not.toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: "비차단 이슈 제외 사유 기록" }),
  ).not.toBeInTheDocument();
});
it("보존된 변경 내용은 최신 revision부터 선택하고 Diff 상태를 표시한다", async () => {
  const file = {
    file_id: "f1",
    path: "a.bin",
    status: "M",
    binary: true,
    truncated: true,
    additions: null,
    deletions: null,
    artifact_id: "artifact-1",
  };
  const changes = vi
    .spyOn(api, "changes")
    .mockImplementation(async (_, revision, sha) => ({
      available: true,
      revision,
      base_sha: "a".repeat(40),
      merge_base_sha: "a".repeat(40),
      candidate_sha: sha ?? "b".repeat(40),
      files: [file],
      history: [
        { revision: 0, candidate_sha: "c".repeat(40) },
        { revision: 1, candidate_sha: "b".repeat(40) },
      ],
    }));
  vi.spyOn(api, "diff").mockResolvedValue({
    ...file,
    base_sha: "a".repeat(40),
    candidate_sha: "b".repeat(40),
    text: "<script>not executable</script>",
  });
  wrap(<ChangesPanel rid="r" version={2} revision={1} />);
  const snapshots = await screen.findByRole<HTMLSelectElement>("combobox", {
    name: "변경 내용 revision 선택",
  });
  const previous = `0:${"c".repeat(40)}`;
  expect(Array.from(snapshots.options, (option) => option.value)).toEqual([
    "",
    `1:${"b".repeat(40)}`,
    previous,
  ]);
  expect(snapshots).toHaveValue("");
  expect(changes).toHaveBeenCalledWith("r", 1, undefined);
  expect(await screen.findByText(/바이너리 파일의 내용/)).toBeVisible();
  expect(screen.getByText(/1 MiB를 초과/)).toBeVisible();
  expect(document.querySelector("script")).toBeNull();
  fireEvent.change(snapshots, { target: { value: previous } });
  expect(
    await screen.findByRole("combobox", { name: "변경 내용 revision 선택" }),
  ).toHaveValue(previous);
  expect(changes).toHaveBeenCalledWith("r", 0, "c".repeat(40));
});

it("코드 수정이 확인된 이슈를 검증 대기로 표시한다", async () => {
  vi.spyOn(api, "reviews").mockResolvedValue({
    status: "available",
    step_id: 2,
    sha: "a".repeat(40),
    revision: 0,
    summary: "review",
    issues: [
      {
        id: "model1",
        issue_id: "issue-host",
        severity: "medium",
        file: "a.py",
        line: 1,
        description: "입력 검증",
        evidence: "기존 오류",
        suggestion: "검증",
        status: "unresolved",
        source_sha: "a".repeat(40),
        resolution_evidence: null,
        status_note: "코드 수정 확인, 동일 SHA·revision 테스트 통과 근거 대기",
        verification_pending: true,
      },
    ],
    history: [],
    legacy_incomplete: false,
    blocking_severities: ["critical", "high"],
  });
  wrap(<ReviewPanel rid="r" version={3} revision={0} canClassify={false} />);
  expect(
    await screen.findByText(
      "코드 수정 확인, 동일 SHA·revision 테스트 통과 근거 대기",
    ),
  ).toBeVisible();
  expect(screen.getByText("검증 대기")).toBeVisible();
  expect(screen.queryByText("미해결")).not.toBeInTheDocument();
  expect(screen.queryByText("독립 재검증으로 해결")).not.toBeInTheDocument();
});

it.each([
  ["affected", "영향 범위 검사", false],
  ["full", "영향 범위에서 전체 검사로 확대", true],
] as const)(
  "실제 %s 범위를 요약하고 명령 선택 근거는 펼쳐서 표시한다",
  async (actual, label, expanded) => {
    vi.spyOn(api, "tests").mockResolvedValue({
      status: "passed",
      step_id: 1,
      sha: "a".repeat(40),
      revision: 0,
      duration_seconds: 1,
      total: 1,
      passed: 1,
      failed: 0,
      skipped: 0,
      commands: [],
      failures: [],
      history: [],
      legacy_incomplete: false,
      selection: {
        selector_version: 1,
        requested_scope: "affected",
        actual_scope: actual,
        expanded,
        reasons: [
          expanded
            ? "공통 코드 변경으로 전체 검사 확대"
            : "누적 변경과 필수 회귀 검사",
        ],
        included: expanded ? ["unit", "unrelated"] : ["unit"],
        excluded: expanded ? [] : ["unrelated"],
        changed_files: ["src/a.py"],
        command_reasons: {
          unit: "변경 입력 매핑으로 선택",
          unrelated: expanded
            ? "전체 검사에 포함"
            : "변경 의존 관계 밖이라 제외",
        },
        base_sha: "b".repeat(40),
        candidate_sha: "a".repeat(40),
        revision: 0,
        policy_hash: "p",
        criteria_hash: "c",
        commands_hash: "m",
        selection_hash: "s",
      },
    });
    wrap(<TestsPanel rid="r" version={2} revision={0} />);
    const scope = await screen.findByLabelText("실제 테스트 범위");
    expect(scope).toHaveTextContent(label);
    expect(scope).toHaveTextContent(
      expanded ? "명령 2개 선택 · 0개 제외" : "명령 1개 선택 · 1개 제외",
    );
    if (expanded) {
      expect(
        within(scope).getByText("공통 코드 변경으로 전체 검사 확대"),
      ).toBeVisible();
    }
    expect(screen.getByText(/제외한 명령 ·/)).not.toBeVisible();
    fireEvent.click(screen.getByText("실행 세부 정보"));
    const evidence = within(
      screen.getByRole("region", { name: "명령 선택 근거" }),
    );
    expect(
      evidence.getByText("요청 범위 · 변경 영향 범위 테스트"),
    ).toBeVisible();
    expect(
      evidence.getByText(
        expanded ? "제외한 명령 · 없음" : "제외한 명령 · unrelated",
      ),
    ).toBeVisible();
    expect(
      evidence.getByText(
        expanded ? /전체 검사에 포함/ : /변경 의존 관계 밖이라 제외/,
      ),
    ).toBeVisible();
    expect(screen.queryByText("전체 테스트 통과")).not.toBeInTheDocument();
  },
);
