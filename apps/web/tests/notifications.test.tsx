import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { Link, MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { createQueryClient } from "../src/App";
import { api } from "../src/api/resources";
import type { Schema } from "../src/api/types";
import { NotificationCenter } from "../src/components/NotificationCenter";
import { Status } from "../src/components/common";

type Page = Schema["NotificationPage"];
const item: Page["items"][number] = {
  message:
    "계속 진행하려면 사용자 확인 또는 입력이 필요합니다. 상세 화면을 확인해 주세요.",
  id: 1,
  run_id: "r1",
  state: "needs_input",
  occurred_at: "2026-11-03T01:00:00Z",
  read: false,
};
const initial: Page = {
  items: [item],
  unread_count: 1,
  cursor: 1,
  next_cursor: null,
};
function mount(path = "/runs/r1") {
  const client = createQueryClient();
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <NotificationCenter />
        <Link to="/projects">프로젝트로 이동</Link>
        <Link to="/runs/r1">상세로 이동</Link>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return client;
}
afterEach(() => vi.restoreAllMocks());

describe("공통 작업 알림", () => {
  it("기존 이력은 조용히 유지하고 새 상태를 비차단 알림으로 한 번 표시한다", async () => {
    const get = vi.spyOn(api, "notifications").mockResolvedValue(initial);
    const alert = vi.spyOn(window, "alert").mockImplementation(() => {});
    const client = mount();
    await screen.findByRole("button", { name: "알림, 읽지 않음 1 개" });
    expect(alert).not.toHaveBeenCalled();
    const next: Page = {
      ...initial,
      cursor: 4,
      unread_count: 2,
      items: [
        {
          ...item,
          id: 4,
          state: "paused",
          message:
            "작업이 일시중지되었습니다. 계속하려면 작업을 재개해 주세요.",
        },
        item,
      ],
    };
    get.mockResolvedValue(next);
    await act(() => client.refetchQueries({ queryKey: ["notifications"] }));
    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent("일시중지"),
    );
    fireEvent.click(screen.getByRole("button", { name: "상태 알림 닫기" }));
    await act(() => client.refetchQueries({ queryKey: ["notifications"] }));
    fireEvent.click(screen.getByText("프로젝트로 이동"));
    get.mockResolvedValue({
      ...next,
      cursor: 5,
      items: [{ ...item, id: 5 }, ...next.items],
    });
    await act(() => client.refetchQueries({ queryKey: ["notifications"] }));
    fireEvent.click(screen.getByText("상세로 이동"));
    expect(alert).not.toHaveBeenCalled();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    client.clear();
  });

  it("종 아이콘에서 이력과 이전 페이지를 확인하고 개별·전체 읽음을 서버에 요청한다", async () => {
    vi.spyOn(api, "notifications").mockImplementation(async (before) =>
      before
        ? { ...initial, items: [{ ...item, id: 1 }], next_cursor: null }
        : {
            ...initial,
            items: [{ ...item, id: 2 }],
            cursor: 2,
            next_cursor: 2,
          },
    );
    const read = vi
      .spyOn(api, "readNotifications")
      .mockResolvedValue(undefined);
    const client = mount("/projects");
    fireEvent.click(
      await screen.findByRole("button", { name: "알림, 읽지 않음 1 개" }),
    );
    expect(await screen.findByRole("dialog")).toBeInTheDocument();
    expect(
      screen.getByText(
        "계속 진행하려면 사용자 확인 또는 입력이 필요합니다. 상세 화면을 확인해 주세요.",
      ),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "이전 알림 더 보기" }));
    await waitFor(() => expect(api.notifications).toHaveBeenCalledWith(2));
    await waitFor(() =>
      expect(screen.getAllByRole("link", { name: /r1/ })).toHaveLength(2),
    );
    fireEvent.click(screen.getByRole("button", { name: "모두 읽음" }));
    await waitFor(() =>
      expect(read).toHaveBeenCalledWith({ through: 2 }, expect.anything()),
    );
    fireEvent.click(screen.getAllByRole("link", { name: /r1/ })[0]);
    await waitFor(() =>
      expect(read).toHaveBeenCalledWith({ ids: [2] }, expect.anything()),
    );
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    client.clear();
  });

  it("다른 작업 이벤트는 현재 상세의 alert를 띄우지 않는다", async () => {
    const get = vi.spyOn(api, "notifications").mockResolvedValue(initial);
    const alert = vi.spyOn(window, "alert").mockImplementation(() => {});
    const client = mount();
    await screen.findByRole("button", { name: "알림, 읽지 않음 1 개" });
    get.mockResolvedValue({
      ...initial,
      cursor: 2,
      items: [{ ...item, id: 2, run_id: "r2" }],
    });
    await act(() => client.refetchQueries({ queryKey: ["notifications"] }));
    expect(alert).not.toHaveBeenCalled();
    client.clear();
  });

  it("이미 읽은 알림을 숨기고 개별 읽음 후 다시 열어도 표시하지 않는다", async () => {
    let read = false;
    vi.spyOn(api, "notifications").mockImplementation(async () => ({
      ...initial,
      unread_count: read ? 0 : 1,
      items: [
        { ...item, read },
        { ...item, id: 2, run_id: "already-read", read: true },
      ],
    }));
    vi.spyOn(api, "readNotifications").mockImplementation(async () => {
      read = true;
    });
    const client = mount("/projects");
    fireEvent.click(
      await screen.findByRole("button", { name: "알림, 읽지 않음 1 개" }),
    );
    expect(await screen.findByRole("link", { name: /r1/ })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /already-read/ })).toBeNull();
    fireEvent.click(screen.getByRole("link", { name: /r1/ }));
    const bell = await screen.findByRole("button", {
      name: "알림, 읽지 않음 0 개",
    });
    fireEvent.click(bell);
    expect(
      await screen.findByText("읽지 않은 알림이 없습니다."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /r1/ })).toBeNull();
    client.clear();
  });

  it("모두 읽음 후 불러온 이전 페이지의 알림까지 목록에서 사라진다", async () => {
    let read = false;
    vi.spyOn(api, "notifications").mockImplementation(async (before) => ({
      ...initial,
      cursor: 2,
      unread_count: read ? 0 : 2,
      items: [{ ...item, id: before ? 1 : 2, read }],
      next_cursor: before ? null : 2,
    }));
    vi.spyOn(api, "readNotifications").mockImplementation(async () => {
      read = true;
    });
    const client = mount("/projects");
    fireEvent.click(
      await screen.findByRole("button", { name: "알림, 읽지 않음 2 개" }),
    );
    fireEvent.click(
      await screen.findByRole("button", { name: "이전 알림 더 보기" }),
    );
    await waitFor(() =>
      expect(screen.getAllByRole("link", { name: /r1/ })).toHaveLength(2),
    );
    fireEvent.click(screen.getByRole("button", { name: "모두 읽음" }));
    expect(
      await screen.findByText("읽지 않은 알림이 없습니다."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /r1/ })).toBeNull();
    expect(screen.getByRole("button", { name: "모두 읽음" })).toBeDisabled();
    client.clear();
  });

  it("상세 상태 표시는 큰 글자와 아이콘을 사용한다", () => {
    render(<Status state="planning" large />);
    expect(screen.getByText("기획 중")).toHaveClass("status-large");
    expect(screen.getByText("기획 중").querySelector("svg")).toHaveAttribute(
      "width",
      "18",
    );
  });
});
