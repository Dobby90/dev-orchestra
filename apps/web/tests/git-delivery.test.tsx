import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, expect, it, vi } from "vitest";
import { api } from "../src/api/resources";
import type { RunDetail, Schema } from "../src/api/types";
import { createQueryClient } from "../src/App";
import { ProjectGitSettings } from "../src/components/ProjectGitSettings";
import { RunPushPanel } from "../src/components/RunPushPanel";

const settings: Schema["GitSettingsView"] = {
  remote: "origin",
  remote_url: "https://example.invalid/team/repo.git",
  account: "account-id",
  token_registered: true,
  version: "v1",
  remotes: [{ name: "origin", url: "https://example.invalid/team/repo.git" }],
  test: null,
};
const preview: Schema["PushView"] = {
  remote: "origin",
  remote_url: settings.remote_url,
  settings_version: "v1",
  branch: "feature/run",
  approved_sha: "a".repeat(40),
  result: null,
};
const run = {
  id: "r1",
  version: 3,
  project_id: "p1",
  state: "completed",
  handoff: { status: "matching" },
  execution: { worktree_removed: false },
} as RunDetail;
function wrap(node: React.ReactNode, client = createQueryClient()) {
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>{node}</MemoryRouter>
    </QueryClientProvider>,
  );
}
beforeEach(() => {
  vi.spyOn(api, "gitSettings").mockResolvedValue({
    ...settings,
    test: {
      status: "success",
      message: "조회 완료",
      url: settings.remote_url!,
      checked_at: "2026-11-12T00:00:00Z",
      push_permission: "unknown",
    },
  });
});
function result(status: string) {
  return {
    ...preview,
    result: {
      status,
      message: "접근 권한을 확인하세요.",
      remote: "origin",
      remote_url: settings.remote_url!,
      branch: preview.branch,
      sha: preview.approved_sha!,
      checked_at: "2026-11-06T12:00:00Z",
    },
  };
}

it("승인 대상 확인 후 수동 Push하고 진행 중 중복 클릭을 막는다", async () => {
  vi.spyOn(api, "pushPreview").mockResolvedValue(preview);
  let resolve!: (value: Schema["PushView"]) => void;
  const request = vi.spyOn(api, "pushBranch").mockImplementation(
    () =>
      new Promise((r) => {
        resolve = r;
      }),
  );
  wrap(<RunPushPanel run={run} />);
  const button = await screen.findByRole("button", {
    name: "작업 브랜치 Push",
  });
  expect(screen.getByText(/대상 작업 브랜치: feature\/run/)).toBeVisible();
  expect(
    screen.getByText(`최종 승인 SHA: ${preview.approved_sha}`),
  ).toBeVisible();
  expect(request).not.toHaveBeenCalled();
  fireEvent.click(button);
  expect(
    await screen.findByRole("button", { name: "Push 진행 중…" }),
  ).toBeDisabled();
  expect(request).toHaveBeenCalledTimes(1);
  expect(request).toHaveBeenCalledWith("r1", 3, {
    settings_version: "v1",
    remote_url: settings.remote_url,
    branch: preview.branch,
    approved_sha: preview.approved_sha,
  });
  vi.mocked(api.pushPreview).mockResolvedValue(result("success"));
  await act(async () => resolve(result("success")));
  expect(screen.queryByRole("button", { name: "Worktree 정리" })).toBeNull();
  expect(request).toHaveBeenCalledTimes(1);
});

it("Push 실패 원인과 재시도 버튼을 표시하고 정리를 자동 실행하지 않는다", async () => {
  vi.spyOn(api, "pushPreview").mockResolvedValue(result("access_denied"));
  const request = vi
    .spyOn(api, "pushBranch")
    .mockResolvedValue(result("success"));
  wrap(<RunPushPanel run={run} />);
  expect(await screen.findByText("접근 권한을 확인하세요.")).toBeVisible();
  fireEvent.click(
    screen.getByRole("button", { name: "작업 브랜치 Push 재시도" }),
  );
  await waitFor(() => expect(request).toHaveBeenCalledTimes(1));
  expect(screen.queryByRole("button", { name: "Worktree 정리" })).toBeNull();
});

it("승인 이후 변경된 브랜치는 Push 버튼을 비활성화한다", async () => {
  vi.spyOn(api, "pushPreview").mockResolvedValue(preview);
  wrap(
    <RunPushPanel
      run={{ ...run, handoff: { ...run.handoff!, status: "changed" } }}
    />,
  );
  expect(
    await screen.findByRole("button", { name: "작업 브랜치 Push" }),
  ).toBeDisabled();
});

it("정리 후에도 Push 성공을 표시하고 Push와 정리 버튼은 제공하지 않는다", async () => {
  vi.spyOn(api, "pushPreview").mockResolvedValue(result("success"));
  wrap(
    <RunPushPanel
      run={{ ...run, execution: { ...run.execution!, worktree_removed: true } }}
    />,
  );
  expect(
    await screen.findByText(/Worktree 정리가 완료되었습니다/),
  ).toBeVisible();
  expect(screen.getByText("작업 브랜치 Push에 성공했습니다.")).toBeVisible();
  expect(screen.queryByRole("button", { name: "Worktree 정리" })).toBeNull();
  expect(screen.queryByRole("button", { name: /Push/ })).toBeNull();
});

it("연결 조회 성공과 Push 권한을 구분하고 설정 편집 시 이전 결과를 숨긴다", async () => {
  vi.spyOn(api, "gitSettings").mockResolvedValue({
    ...settings,
    test: {
      status: "success",
      message: "조회 완료",
      url: settings.remote_url!,
      checked_at: "2026-11-06",
      push_permission: "unknown",
    },
  });
  const test = vi.spyOn(api, "testGit").mockResolvedValue(settings);
  wrap(<ProjectGitSettings pid="p1" />);
  expect(await screen.findByText("저장소 조회 성공")).toBeVisible();
  expect(screen.getByText(/Push 권한: 확인되지 않음/)).toBeVisible();
  expect(screen.getByLabelText("인증 토큰 등록·교체")).toHaveValue("");
  fireEvent.change(screen.getByLabelText("Git 인증 계정 ID"), {
    target: { value: "changed" },
  });
  expect(screen.queryByText("저장소 조회 성공")).toBeNull();
  expect(screen.getByRole("button", { name: "연결 테스트" })).toBeDisabled();
  expect(test).not.toHaveBeenCalled();
});

it("토큰 교체는 저장 요청으로만 전달하고 입력을 비운다", async () => {
  vi.spyOn(api, "gitSettings").mockResolvedValue(settings);
  const save = vi
    .spyOn(api, "saveGitSettings")
    .mockResolvedValue({ ...settings, version: "v2" });
  wrap(<ProjectGitSettings pid="p1" />);
  const input = await screen.findByLabelText("인증 토큰 등록·교체");
  expect(input).toHaveAttribute("type", "password");
  fireEvent.change(input, { target: { value: "CANARY_TOKEN" } });
  fireEvent.click(screen.getByRole("button", { name: "Git 설정 저장" }));
  await waitFor(() =>
    expect(save).toHaveBeenCalledWith("p1", {
      expected_version: "v1",
      remote: "origin",
      account: "account-id",
      token: "CANARY_TOKEN",
      delete_token: false,
    }),
  );
  await waitFor(() =>
    expect(screen.getByLabelText("인증 토큰 등록·교체")).toHaveValue(""),
  );
  expect(localStorage.length).toBe(0);
});

it.each([
  [
    "원격 미설정",
    { ...settings, remote_url: null },
    "원격 저장소를 설정하세요",
  ],
  ["연결 미확인", settings, "연결 테스트를 실행하세요"],
  [
    "인증 실패",
    {
      ...settings,
      test: {
        status: "authentication_failed",
        message: "인증 정보를 확인하세요.",
        url: settings.remote_url!,
        checked_at: "2026-11-12",
        push_permission: "unknown" as const,
      },
    },
    "인증 정보를 확인하세요",
  ],
] as const)(
  "%s이면 Push를 차단하고 설정 링크를 제공한다",
  async (_, value, message) => {
    vi.mocked(api.gitSettings).mockResolvedValue(value);
    vi.spyOn(api, "pushPreview").mockResolvedValue(preview);
    const request = vi.spyOn(api, "pushBranch");
    wrap(<RunPushPanel run={run} />);
    expect(await screen.findByText(new RegExp(message))).toBeVisible();
    const button = screen.getByRole("button", { name: "작업 브랜치 Push" });
    expect(button).toBeDisabled();
    fireEvent.click(button);
    expect(request).not.toHaveBeenCalled();
    expect(
      screen.getByRole("link", { name: "Git 계정·인증 설정" }),
    ).toHaveAttribute("href", "/projects/p1?tab=git");
  },
);

it("토큰 없는 SSH·helper 연결도 재확인 후 Push를 활성화한다", async () => {
  vi.mocked(api.gitSettings).mockResolvedValue({
    ...settings,
    account: "",
    token_registered: false,
  });
  vi.spyOn(api, "pushPreview").mockResolvedValue(preview);
  vi.spyOn(api, "testGit").mockResolvedValue({
    ...settings,
    account: "",
    token_registered: false,
    test: {
      status: "success",
      message: "조회 완료",
      url: settings.remote_url!,
      checked_at: "2026-11-12",
      push_permission: "unknown",
    },
  });
  wrap(<RunPushPanel run={run} />);
  expect(
    await screen.findByRole("button", { name: "작업 브랜치 Push" }),
  ).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Git 연결 테스트" }));
  await waitFor(() =>
    expect(
      screen.getByRole("button", { name: "작업 브랜치 Push" }),
    ).toBeEnabled(),
  );
  expect(api.testGit).toHaveBeenCalledWith("p1", "v1");
});

it("연결 성공 이후 발생한 인증 실패는 연결 재확인까지 Push를 막는다", async () => {
  const failed = result("authentication_failed");
  failed.result.checked_at = "2026-11-13T00:00:00Z";
  vi.spyOn(api, "pushPreview").mockResolvedValue(failed);
  wrap(<RunPushPanel run={run} />);
  expect(await screen.findByText(/마지막 Push의 인증·접근 오류/)).toBeVisible();
  expect(
    screen.getByRole("button", { name: "작업 브랜치 Push 재시도" }),
  ).toBeDisabled();
});

it("외부에서 연결 설정이 변경되면 기존 연결 성공으로 Push를 허용하지 않는다", async () => {
  const client = createQueryClient();
  vi.spyOn(api, "pushPreview").mockResolvedValue(preview);
  wrap(<RunPushPanel run={run} />, client);
  await waitFor(() =>
    expect(
      screen.getByRole("button", { name: "작업 브랜치 Push" }),
    ).toBeEnabled(),
  );
  vi.mocked(api.gitSettings).mockResolvedValue({ ...settings, version: "v2" });
  vi.mocked(api.pushPreview).mockResolvedValue({
    ...preview,
    settings_version: "v2",
  });
  await act(async () => {
    await client.invalidateQueries({ queryKey: ["project-git", "p1"] });
  });
  expect(
    await screen.findByText("Push 전에 Git 연결 테스트를 실행하세요."),
  ).toBeVisible();
  expect(
    screen.getByRole("button", { name: "작업 브랜치 Push" }),
  ).toBeDisabled();
});

it("연결 테스트 요청 자체가 실패해도 Push는 비활성 상태를 유지한다", async () => {
  vi.spyOn(api, "pushPreview").mockResolvedValue(preview);
  vi.spyOn(api, "testGit").mockRejectedValue(new Error("연결 오류"));
  wrap(<RunPushPanel run={run} />);
  await waitFor(() =>
    expect(
      screen.getByRole("button", { name: "Git 연결 테스트" }),
    ).toBeEnabled(),
  );
  fireEvent.click(screen.getByRole("button", { name: "Git 연결 테스트" }));
  expect(
    await screen.findByText("Git 연결 테스트에 실패했습니다. 다시 확인하세요."),
  ).toBeVisible();
  expect(
    screen.getByRole("button", { name: "작업 브랜치 Push" }),
  ).toBeDisabled();
});
