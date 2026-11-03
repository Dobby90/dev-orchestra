import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { expect, it, vi } from "vitest";
import { api } from "../src/api/resources";
import { ApiError } from "../src/api/client";
import { createQueryClient } from "../src/App";
import { TestChangePanel } from "../src/components/TestChangePanel";
import type { RunDetail, Schema } from "../src/api/types";

const run: RunDetail = {
  schema_version: 1,
  id: "r1",
  state: "needs_input",
  version: 12,
  branch: "feature/xml",
  base_sha: "a".repeat(40),
  current_sha: "a".repeat(40),
  final_sha: null,
  iteration: 0,
  revision: 0,
  current_role: "developer",
  control: null,
  event_cursor: 0,
  project_id: "p1",
  base_branch: "main",
  created_at: null,
  max_fix_attempts: 3,
  questions: [],
  error: null,
  deletion_pending: false,
  test_change_pending: true,
  allowed_actions: ["approve-test-changes", "reject-test-changes"],
};
const proposal: Schema["TestChangeProposalView"] = {
  id: "proposal-1",
  proposal_hash: "b".repeat(64),
  input_sha: "a".repeat(40),
  patch_hash: "c".repeat(64),
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
  created_at: "2026-11-03T01:00:00Z",
  patch: "--- tests/xml.spec.ts\n-disabled\n+enabled <script>CANARY</script>",
  approved: false,
};
const operation: Schema["OperationView"] = {
  id: "op-1",
  run_id: "r1",
  kind: "approve-test-changes",
  status: "applied",
  state: "queued",
  result: {},
  created_at: "2026-11-03",
  applied_at: "2026-11-03",
  error: null,
};
function mount(value = run) {
  const client = createQueryClient();
  return render(
    <QueryClientProvider client={client}>
      <TestChangePanel run={value} />
    </QueryClientProvider>,
  );
}

it("변경 사유와 전체 diff를 보여주고 정확한 제안·실행 버전으로 승인한다", async () => {
  vi.spyOn(api, "testChanges").mockResolvedValue({ proposal });
  const control = vi.spyOn(api, "control").mockResolvedValue(operation);
  mount();
  expect(await screen.findByText("XML 탭 활성화 기대값 갱신")).toBeVisible();
  expect(document.querySelector("script")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "테스트 변경 승인" }));
  const dialog = await screen.findByRole("dialog");
  expect(dialog.querySelector("pre")?.textContent).toBe(proposal.patch);
  expect(control).not.toHaveBeenCalled();
  fireEvent.click(
    within(dialog).getByRole("button", { name: "승인하고 재개" }),
  );
  await waitFor(() =>
    expect(control).toHaveBeenCalledWith(
      run.id,
      run.version,
      "approve-test-changes",
      {
        proposal_hash: proposal.proposal_hash,
        input_sha: proposal.input_sha,
        reason: "",
      },
      expect.any(String),
    ),
  );
});

it("반려 사유를 요구하며 API로 전달한다", async () => {
  vi.spyOn(api, "testChanges").mockResolvedValue({ proposal });
  const control = vi.spyOn(api, "control").mockResolvedValue(operation);
  mount();
  fireEvent.click(
    await screen.findByRole("button", { name: "반려·다시 작성" }),
  );
  const input = screen.getByLabelText("반려 사유·수정 지시");
  expect(input).toBeRequired();
  fireEvent.change(input, { target: { value: "YAML 회귀 검사를 유지하세요" } });
  fireEvent.click(screen.getByRole("button", { name: "반려하고 다시 작성" }));
  await waitFor(() =>
    expect(control).toHaveBeenCalledWith(
      run.id,
      run.version,
      "reject-test-changes",
      {
        proposal_hash: proposal.proposal_hash,
        input_sha: proposal.input_sha,
        reason: "YAML 회귀 검사를 유지하세요",
      },
      expect.any(String),
    ),
  );
});

it("실행 버전이 바뀌면 열린 승인 대화상자에서 제출을 막는다", async () => {
  vi.spyOn(api, "testChanges").mockResolvedValue({ proposal });
  const control = vi.spyOn(api, "control").mockResolvedValue(operation);
  const client = createQueryClient();
  const node = (value: RunDetail) => (
    <QueryClientProvider client={client}>
      <TestChangePanel run={value} />
    </QueryClientProvider>
  );
  const view = render(node(run));
  fireEvent.click(
    await screen.findByRole("button", { name: "테스트 변경 승인" }),
  );
  view.rerender(node({ ...run, version: 13 }));
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "작업 상태가 변경",
  );
  expect(screen.getByRole("button", { name: "승인하고 재개" })).toBeDisabled();
  expect(control).not.toHaveBeenCalled();
});

it("응답을 잃으면 같은 승인 요청 키와 본문으로 재확인한다", async () => {
  vi.spyOn(api, "testChanges").mockResolvedValue({ proposal });
  const control = vi
    .spyOn(api, "control")
    .mockRejectedValueOnce(new ApiError(0, "NETWORK_ERROR"))
    .mockResolvedValue(operation);
  mount();
  fireEvent.click(
    await screen.findByRole("button", { name: "테스트 변경 승인" }),
  );
  fireEvent.click(screen.getByRole("button", { name: "승인하고 재개" }));
  fireEvent.click(
    await screen.findByRole("button", { name: "같은 요청 재확인" }),
  );
  await waitFor(() => expect(control).toHaveBeenCalledTimes(2));
  expect(control.mock.calls[0]).toEqual(control.mock.calls[1]);
});

it("손상된 제안은 승인을 숨기고 반려로 다시 작성할 수 있게 한다", async () => {
  vi.spyOn(api, "testChanges").mockResolvedValue({
    proposal: { ...proposal, patch: "" },
    error: "제안이 변경되어 승인할 수 없습니다.",
  });
  mount();
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "승인할 수 없습니다",
  );
  expect(
    screen.queryByRole("button", { name: "테스트 변경 승인" }),
  ).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "반려·다시 작성" })).toBeEnabled();
});
