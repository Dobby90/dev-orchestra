import { render, screen } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { expect, it, vi } from "vitest";
import { api } from "../src/api/resources";
import { createQueryClient } from "../src/App";
import {
  TestsPanel,
  ReviewPanel,
  ChangesPanel,
} from "../src/components/ResultPanels";
import type { Schema } from "../src/api/types";
function wrap(node: React.ReactNode) {
  return render(
    <QueryClientProvider client={createQueryClient()}>
      {node}
    </QueryClientProvider>,
  );
}
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
        failure_details: [
          {
            name: "broken",
            message: "mismatch",
            details: "<script>CANARY</script>",
          },
        ],
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
it("보존된 Diff의 binary·잘림과 텍스트 안전성을 표시한다", async () => {
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
  vi.spyOn(api, "changes").mockResolvedValue({
    available: true,
    revision: 0,
    base_sha: "a".repeat(40),
    merge_base_sha: "a".repeat(40),
    candidate_sha: "b".repeat(40),
    files: [file],
    history: [],
  });
  vi.spyOn(api, "diff").mockResolvedValue({
    ...file,
    base_sha: "a".repeat(40),
    candidate_sha: "b".repeat(40),
    text: "<script>not executable</script>",
  });
  wrap(<ChangesPanel rid="r" version={2} revision={0} />);
  expect(await screen.findByText(/바이너리 파일의 내용/)).toBeVisible();
  expect(screen.getByText(/1 MiB를 초과/)).toBeVisible();
  expect(document.querySelector("script")).toBeNull();
});
