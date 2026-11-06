import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { createQueryClient } from "../src/App";
import { DeleteRunButton } from "../src/components/DeleteRunButton";
import { api } from "../src/api/resources";
import { ApiError } from "../src/api/client";
import type { RunDetail } from "../src/api/types";

const run: RunDetail = {
  name: "r1",
  worktree_removed: false,
  push_status: "not_pushed",
  test_scope: "full",
  id: "run-1234567890abcdef",
  state: "cancelled",
  version: 4,
  current_sha: null,
  final_sha: null,
  base_sha: "a".repeat(40),
  branch: "feature/run",
  iteration: 0,
  current_role: null,
  control: "cancel",
  revision: 0,
  event_cursor: 0,
  project_id: "project-1",
  base_branch: "main",
  created_at: null,
  max_fix_attempts: 3,
  questions: [],
  error: null,
  schema_version: 1,
  deletion_pending: false,
  test_change_pending: false,
  test_change_count: 0,
};

function mount(value = run) {
  const client = createQueryClient();
  client.setQueryData(["runs", ""], { items: [run] });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[`/runs/${run.id}`]}>
        <Routes>
          <Route path="/" element={<p>작업 목록</p>} />
          <Route path="/runs/:id" element={<DeleteRunButton run={value} />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return client;
}

describe("작업 기록 삭제", () => {
  it("최신 버전과 삭제 범위를 확인한 뒤 삭제하고 목록으로 돌아간다", async () => {
    vi.spyOn(api, "run").mockResolvedValue({ ...run, version: 5 });
    const remove = vi.spyOn(api, "deleteRun").mockResolvedValue(undefined);
    const client = mount();
    fireEvent.click(screen.getByRole("button", { name: "작업 기록 삭제" }));
    expect(await screen.findByRole("dialog")).toHaveTextContent("로그·보고서");
    expect(screen.getByRole("dialog")).toHaveTextContent("승인 근거");
    expect(screen.getByRole("dialog")).toHaveTextContent(
      "로컬 작업 브랜치를 영구",
    );
    expect(screen.getByRole("dialog")).toHaveTextContent(
      "삭제할 로컬 브랜치: feature/run",
    );
    expect(remove).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "영구 삭제 확인" }));
    expect(await screen.findByText("작업 목록")).toBeVisible();
    expect(remove).toHaveBeenCalledWith(run.id, 5);
    expect(client.getQueryState(["runs", ""])?.isInvalidated).toBe(true);
    expect(client.getQueryData(["run", run.id])).toBeUndefined();
  });

  it("확인창을 닫으면 삭제하지 않는다", async () => {
    vi.spyOn(api, "run").mockResolvedValue(run);
    const remove = vi.spyOn(api, "deleteRun").mockResolvedValue(undefined);
    mount();
    fireEvent.click(screen.getByText("작업 기록 삭제"));
    await screen.findByRole("dialog");
    fireEvent.click(screen.getByLabelText("대화상자 닫기"));
    expect(remove).not.toHaveBeenCalled();
  });

  it("실패 후 최신 버전으로 다시 확인하고 재시도한다", async () => {
    const get = vi
      .spyOn(api, "run")
      .mockResolvedValueOnce(run)
      .mockResolvedValue({ ...run, version: 5, deletion_pending: true });
    const remove = vi
      .spyOn(api, "deleteRun")
      .mockRejectedValueOnce(new ApiError(409, "RUN_DELETE_FAILED"))
      .mockResolvedValue(undefined);
    mount({ ...run, deletion_pending: true });
    fireEvent.click(screen.getByText("기록 삭제 재시도"));
    fireEvent.click(await screen.findByText("영구 삭제 확인"));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "일부 파일이 삭제되었을 수 있습니다",
    );
    fireEvent.click(screen.getByText("기록 삭제 재시도"));
    fireEvent.click(await screen.findByText("영구 삭제 확인"));
    await screen.findByText("작업 목록");
    expect(get).toHaveBeenCalledTimes(2);
    expect(remove).toHaveBeenLastCalledWith(run.id, 5);
  });

  it("네트워크 오류 후 서버에서 삭제가 확인되면 목록으로 돌아간다", async () => {
    vi.spyOn(api, "run")
      .mockResolvedValueOnce(run)
      .mockRejectedValue(new ApiError(404, "RUN_NOT_FOUND"));
    vi.spyOn(api, "deleteRun").mockRejectedValue(new ApiError(0, "NETWORK"));
    mount();
    fireEvent.click(screen.getByText("작업 기록 삭제"));
    fireEvent.click(await screen.findByText("영구 삭제 확인"));
    await screen.findByRole("alert");
    fireEvent.click(screen.getByText("작업 기록 삭제"));
    await screen.findByText("작업 목록");
  });

  it("최신 상태가 삭제 가능한 상태가 아니면 확인 버튼을 막는다", async () => {
    vi.spyOn(api, "run").mockResolvedValue({ ...run, state: "needs_input" });
    const remove = vi.spyOn(api, "deleteRun").mockResolvedValue(undefined);
    mount();
    fireEvent.click(screen.getByText("작업 기록 삭제"));
    expect(await screen.findByText("영구 삭제 확인")).toBeDisabled();
    await waitFor(() => expect(remove).not.toHaveBeenCalled());
  });
});
