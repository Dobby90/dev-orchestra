import { render, screen } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { expect, it, vi } from "vitest";
import { api } from "../src/api/resources";
import { createQueryClient } from "../src/App";
import { TestChangePanel } from "../src/components/TestChangePanel";
import type { RunDetail, Schema } from "../src/api/types";

const run: RunDetail = {
  name: "r1",
  worktree_removed: false,
  push_status: "not_pushed",
  test_scope: "full",
  schema_version: 1,
  id: "r1",
  state: "waiting_approval",
  version: 12,
  branch: "feature/xml",
  base_sha: "a".repeat(40),
  current_sha: "b".repeat(40),
  final_sha: "b".repeat(40),
  iteration: 0,
  revision: 0,
  current_role: null,
  control: null,
  event_cursor: 0,
  project_id: "p1",
  base_branch: "main",
  created_at: null,
  max_fix_attempts: 3,
  questions: [],
  error: null,
  deletion_pending: false,
  test_change_pending: false,
  test_change_count: 1,
  allowed_actions: ["approve", "request-changes"],
};
const record: Schema["TestChangeRecordView"] = {
  id: "change-1",
  input_sha: "a".repeat(40),
  output_sha: "b".repeat(40),
  role: "developer",
  revision: 0,
  iteration: 0,
  changes: [
    {
      file: "tests/xml.spec.ts",
      criterion_ids: ["AC1"],
      reason: "XML 탭 활성화 기대값 갱신",
    },
  ],
  patch: "--- tests/xml.spec.ts\n-disabled\n+enabled <script>CANARY</script>",
};
function mount(value = run) {
  return render(
    <QueryClientProvider client={createQueryClient()}>
      <TestChangePanel run={value} />
    </QueryClientProvider>,
  );
}

it("자동 변경 근거와 전체 diff를 최종 검토용으로 표시하며 중간 승인을 요구하지 않는다", async () => {
  vi.spyOn(api, "testChanges").mockResolvedValue({
    proposal: null,
    error: null,
    history: [record],
  });
  const control = vi.spyOn(api, "control");
  mount();
  expect(await screen.findByText("XML 탭 활성화 기대값 갱신")).toBeVisible();
  expect(document.querySelector("pre")?.textContent).toBe(record.patch);
  expect(document.querySelector("script")).toBeNull();
  expect(screen.queryByRole("button")).not.toBeInTheDocument();
  expect(control).not.toHaveBeenCalled();
});

it("이전 버전의 보관 패치는 일반 재개로 자동 처리할 수 있음을 안내한다", async () => {
  vi.spyOn(api, "testChanges").mockResolvedValue({
    proposal: {
      ...record,
      proposal_hash: "c".repeat(64),
      patch_hash: "d".repeat(64),
      created_at: "2026-11-03T01:00:00Z",
      approved: false,
    },
    error: null,
    history: [],
  });
  mount({ ...run, state: "needs_input", test_change_pending: true });
  expect(
    await screen.findByText(/이전 버전에서 보관한 패치입니다/),
  ).toHaveTextContent("재개하면");
  expect(screen.getByText("XML 탭 활성화 기대값 갱신")).toBeVisible();
  expect(
    screen.queryByRole("button", { name: "테스트 변경 승인" }),
  ).not.toBeInTheDocument();
});

it("무결성을 확인하지 못한 변경 내역은 오류로 표시한다", async () => {
  vi.spyOn(api, "testChanges").mockResolvedValue({
    proposal: null,
    history: [],
    error: "기존 테스트 변경 근거를 확인할 수 없습니다.",
  });
  mount();
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "근거를 확인할 수 없습니다",
  );
});
