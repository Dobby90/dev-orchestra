import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { expect, it, vi } from "vitest";
import { LogViewer } from "../src/components/LogViewer";
import { describeLog, logLines } from "../src/components/logPresentation";
import { api } from "../src/api/resources";
import type { Log } from "../src/hooks/events";
const { virtual } = vi.hoisted(() => ({
  virtual: {
    scrollToIndex: vi.fn(),
    getTotalSize: () => 54,
    getVirtualItems: () => [],
    measureElement: vi.fn(),
  },
}));
vi.mock("@tanstack/react-virtual", () => ({ useVirtualizer: () => virtual }));
const log: Log = {
  id: 1,
  text: "first\n",
  source: "process.log",
  role: "developer",
  level: "unknown",
  step: 42,
};
it("보관량이 일정해도 최신 로그를 따라가고 위로 읽는 동안 새 로그 수를 표시한다", () => {
  const node = (id: number) => (
    <LogViewer logs={[{ ...log, id }]} removed={id - 1} unavailable={false} />
  );
  const view = render(node(1));
  virtual.scrollToIndex.mockClear();
  view.rerender(node(2));
  expect(virtual.scrollToIndex).toHaveBeenCalled();
  fireEvent.wheel(screen.getByLabelText("실행 로그"), { deltaY: -50 });
  virtual.scrollToIndex.mockClear();
  view.rerender(node(3));
  expect(virtual.scrollToIndex).not.toHaveBeenCalled();
  fireEvent.click(
    screen.getByRole("button", { name: /새 로그 1개 · 최신으로/ }),
  );
  expect(virtual.scrollToIndex).toHaveBeenCalled();
});
it("출처·generation·offset이 이어지는 JSON만 합치고 원문을 보존한다", () => {
  const raw =
    JSON.stringify({ item: { type: "agent_message", text: "한글 메시지" } }) +
    "\n";
  const first = {
    ...log,
    logId: "a",
    generation: 1,
    offset: 0,
    endOffset: 10,
    text: raw.slice(0, 10),
  };
  const second = {
    ...first,
    id: 2,
    offset: 10,
    endOffset: 30,
    text: raw.slice(10),
  };
  const rows = logLines([first, second]);
  expect(rows).toHaveLength(1);
  expect(rows[0].text).toBe(raw);
  expect(describeLog(rows[0].text)).toEqual({
    label: "메시지",
    body: "한글 메시지",
  });
  expect(logLines([first, { ...second, generation: 2 }])).toHaveLength(2);
  expect(logLines([first, { ...second, offset: 11 }])).toHaveLength(2);
  expect(describeLog(raw.slice(0, -1))).toBeNull();
  expect(describeLog("plain\n")).toBeNull();
});
it("단계 선택 시 보관 범위 밖의 해당 단계 로그도 조회한다", async () => {
  const window = vi.spyOn(api, "logWindow").mockResolvedValue({
    items: [],
    latest_cursor: 100,
    before_cursor: 10,
    has_more: false,
  });
  render(
    <MemoryRouter>
      <LogViewer
        runId="r1"
        stepId={42}
        logs={[]}
        removed={0}
        unavailable={false}
        state="completed"
      />
    </MemoryRouter>,
  );
  await waitFor(() => expect(window).toHaveBeenCalledWith("r1", undefined, 42));
  expect(screen.getByText("필터 결과가 없습니다")).toBeVisible();
});
it("종료된 작업의 빈 로그와 연결 상태를 구분한다", () => {
  render(
    <LogViewer
      logs={[]}
      removed={0}
      unavailable={false}
      state="completed"
      connection="reconnecting"
      lastReceived="2026-11-03T01:00:00Z"
    />,
  );
  expect(screen.getByText("저장된 로그가 없습니다")).toBeVisible();
  expect(screen.getByRole("status")).toHaveTextContent(
    "재연결 중 · 주기적 조회",
  );
});
