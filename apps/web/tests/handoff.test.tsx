import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import { RunHandoffPanel } from "../src/components/RunHandoffPanel";
import type { RunDetail } from "../src/api/types";

const run = {
  state: "completed",
  test_scope: "affected",
  handoff: {
    repository: "/projects/example",
    branch: "custom/work",
    approved_sha: "approved",
    current_branch_sha: "current",
    status: "changed",
    commands: ["git branch --list", "git log", "git diff"],
    notice:
      "Worktree 정리 후 재개할 수 없습니다. 브랜치·보고서·검증 이력은 보존됩니다.",
  },
  capacity: {
    active: 2,
    total: 3,
    repository_active: 1,
    repository_limit: 2,
    wait_reason: "repository_capacity",
  },
} as RunDetail;
it("실제 브랜치와 과거 승인 SHA·현재 ref 차이 및 정리 경계를 표시한다", () => {
  render(<RunHandoffPanel run={run} />);
  expect(screen.getByText("로컬 브랜치: custom/work")).toBeVisible();
  expect(screen.getByText("최종 승인 SHA: approved")).toBeVisible();
  expect(screen.getByText("현재 브랜치 SHA: current")).toBeVisible();
  expect(screen.getByRole("alert")).toHaveTextContent("과거 승인 SHA");
  expect(screen.getByText(/정리 후 재개할 수 없습니다/)).toBeVisible();
  expect(screen.getByText("선택한 테스트")).toBeVisible();
  expect(screen.getByText("변경 영향 범위")).toBeVisible();
  expect(
    screen.getByText("테스트·리뷰 안내").closest("details"),
  ).not.toHaveAttribute("open");
});
it.each(["total_capacity", "repository_capacity", "shared_resource"] as const)(
  "%s 대기를 구분한다",
  (reason) => {
    render(
      <RunHandoffPanel
        run={{ ...run, capacity: { ...run.capacity!, wait_reason: reason } }}
      />,
    );
    expect(screen.getByRole("status")).toHaveTextContent(
      {
        total_capacity: "전체 실행 용량 대기",
        repository_capacity: "동일 저장소 실행 용량 대기",
        shared_resource: "공유 테스트 자원 대기",
      }[reason],
    );
  },
);
it.each(["metadata", "same_role"] as const)(
  "%s 복구 방식과 보존 파일을 표시한다",
  (recovery) => {
    render(
      <RunHandoffPanel
        run={{
          ...run,
          workspace_progress: {
            commits: [{ sha: "checkpoint", summary: "작업" }],
            checkpoint_sha: "checkpoint",
            preserved_changes: 2,
            recovery,
          },
        }}
      />,
    );
    expect(screen.getByText(/보존된 변경 파일 2개/)).toBeVisible();
    expect(screen.getByText("복구 지점: checkpoint")).toBeVisible();
    expect(
      screen.getByText(
        recovery === "metadata"
          ? /결과 요약만 보완/
          : /같은 역할·시도를 이어갑니다/,
      ),
    ).toBeVisible();
  },
);
