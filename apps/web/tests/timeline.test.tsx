import { render, screen, fireEvent } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { expect, it, vi } from "vitest";
import { createQueryClient } from "../src/App";
import { api } from "../src/api/resources";
import { WorkflowTimeline } from "../src/components/WorkflowTimeline";
import { TestsPanel, ReviewPanel } from "../src/components/ResultPanels";
import type { RunDetail, Schema } from "../src/api/types";
const run = {
  id: "r",
  state: "testing",
  version: 1,
  revision: 1,
  iteration: 1,
} as RunDetail;
const step: Schema["StepView"] = {
  id: 42,
  run_id: "r",
  role: "tester",
  revision: 1,
  iteration: 1,
  attempt: 2,
  status: "completed",
  input_sha: "abc",
  output_sha: "def",
  started_at: "2026-11-03T01:00:00Z",
  finished_at: "2026-11-03T01:01:05Z",
};
function wrap(node: React.ReactNode) {
  return render(
    <QueryClientProvider client={createQueryClient()}>
      {node}
    </QueryClientProvider>,
  );
}
it("실행 완료와 실패한 검증을 구분하고 정확한 시도로 연결한다", async () => {
  const tests = vi
    .spyOn(api, "tests")
    .mockResolvedValue({ status: "failed" } as Schema["TestResultView"]);
  const select = vi.fn();
  wrap(<WorkflowTimeline run={run} items={[step]} onSelect={select} />);
  expect(await screen.findByText("테스트 실패")).toBeVisible();
  expect(screen.getByText("실행 완료")).toBeVisible();
  expect(tests).toHaveBeenCalledWith("r", 42);
  expect(screen.getByText(/1분 5초 소요/)).toBeVisible();
  fireEvent.click(screen.getByText("해당 시도 결과"));
  expect(select).toHaveBeenCalledWith("tests", 42);
  fireEvent.click(screen.getByText("단계 로그"));
  expect(select).toHaveBeenCalledWith("logs", 42);
  fireEvent.click(screen.getByText("시간·SHA 상세"));
  expect(screen.getByText("출력 SHA: def")).toBeVisible();
});
it("이전 revision은 접고 현재 회차를 펼치며 누락 시간은 미상으로 표시한다", () => {
  wrap(
    <WorkflowTimeline
      run={run}
      items={[
        { ...step, id: 1, revision: 0 },
        { ...step, role: "developer", started_at: null },
      ]}
      onSelect={vi.fn()}
    />,
  );
  const groups = document.querySelectorAll(".timeline-group");
  expect(groups[0]).not.toHaveAttribute("open");
  expect(groups[1]).toHaveAttribute("open");
  expect(screen.getByText(/시간 미상/)).toBeVisible();
});
it.each([
  ["developing", "승인 단계 전"],
  ["waiting_approval", "승인 대기"],
  ["completed", "승인 완료"],
  ["cancelled", "작업 종료로 미진행"],
  ["failed", "작업 종료로 미진행"],
] as const)("%s 상태의 승인 노드를 구분한다", (state, label) => {
  wrap(
    <WorkflowTimeline run={{ ...run, state }} items={[]} onSelect={vi.fn()} />,
  );
  expect(document.querySelector(".approval-step")).toHaveTextContent(label);
});
it("진행 중인 단계에 현재 위치를 표시하고 알 수 없는 상태를 중단으로 단정하지 않는다", () => {
  wrap(
    <WorkflowTimeline
      run={run}
      items={[
        { ...step, role: "developer", status: "running", finished_at: null },
        { ...step, id: 43, role: "developer", status: "other" },
      ]}
      onSelect={vi.fn()}
    />,
  );
  expect(document.querySelector('[aria-current="step"]')).toHaveTextContent(
    "진행 중",
  );
  expect(screen.getByText("상태 미상")).toBeVisible();
});
it("결과 패널이 연결받은 시도의 근거를 조회한다", async () => {
  const tests = vi.spyOn(api, "tests").mockResolvedValue({
    status: "failed",
    history: [],
    commands: [],
    failures: [],
    revision: 0,
  } as unknown as Schema["TestResultView"]);
  const reviews = vi.spyOn(api, "reviews").mockResolvedValue({
    status: "not_run",
    history: [],
    issues: [],
  } as unknown as Schema["ReviewResultView"]);
  wrap(
    <>
      <TestsPanel rid="r" version={1} revision={1} initialStep="42" />
      <ReviewPanel
        rid="r"
        version={1}
        revision={1}
        initialStep="43"
        canClassify={false}
      />
    </>,
  );
  await screen.findByRole("heading", { name: "테스트 실패" });
  expect(tests).toHaveBeenCalledWith("r", 42);
  expect(reviews).toHaveBeenCalledWith("r", 43);
});

it("이력만 별도 스크롤 영역에 넣고 요약과 승인은 밖에 유지한다", () => {
  wrap(<WorkflowTimeline run={run} items={[]} onSelect={vi.fn()} />);
  const history = screen.getByRole("region", { name: "워크플로 실행 이력" });
  expect(history).toHaveAttribute("tabindex", "0");
  expect(history).not.toContainElement(
    document.querySelector(".workflow-current"),
  );
  expect(history).not.toContainElement(
    document.querySelector(".approval-step"),
  );
  expect(screen.queryByRole("button", { name: "현재 단계로 이동" })).toBeNull();
});
it("접힌 현재 회차를 펼쳐 이동하고 이후 갱신에는 위치를 바꾸지 않는다", async () => {
  const { waitFor } = await import("@testing-library/react");
  const client = createQueryClient();
  const items = [
    { ...step, role: "developer", status: "running", finished_at: null },
  ];
  const node = () => (
    <QueryClientProvider client={client}>
      <WorkflowTimeline run={run} items={[...items]} onSelect={vi.fn()} />
    </QueryClientProvider>
  );
  const view = render(node());
  const group = document.querySelector(".timeline-group") as HTMLDetailsElement;
  group.open = false;
  fireEvent(group, new Event("toggle"));
  expect(group).not.toHaveAttribute("open");
  const scroll = vi.mocked(Element.prototype.scrollTo);
  scroll.mockClear();
  fireEvent.click(screen.getByRole("button", { name: "현재 단계로 이동" }));
  await waitFor(() => expect(group).toHaveAttribute("open"));
  await waitFor(() => expect(scroll).toHaveBeenCalledTimes(1));
  view.rerender(node());
  expect(scroll).toHaveBeenCalledTimes(1);
});
