import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { expect, it, vi } from "vitest";
import { api } from "../src/api/resources";
import { SettingsPage } from "../src/pages/SettingsPage";

it("서비스 상태를 첫 탭으로 표시하고 전환해도 미저장 모델을 유지한다", async () => {
  vi.spyOn(api, "health").mockResolvedValue({ status: "ok" });
  vi.spyOn(api, "agentSettings").mockResolvedValue({
    version: "v",
    settings: { roles: {} },
  });
  vi.spyOn(api, "webhookSettings").mockResolvedValue({
    version: "v",
    enabled: false,
    app: "",
    url_configured: false,
    apps: [],
    last_delivery: null,
  });
  vi.spyOn(api, "codexModels").mockResolvedValue({
    items: [{ model: "test-model", reasoning_efforts: ["high"] }],
  });
  const save = vi.spyOn(api, "saveAgentSettings");
  render(
    <QueryClientProvider client={new QueryClient()}>
      <SettingsPage />
    </QueryClientProvider>,
  );
  expect(screen.getAllByRole("tab").map((tab) => tab.textContent)).toEqual([
    "서비스 상태",
    "에이전트 모델",
    "웹훅 알림",
  ]);
  expect(screen.getByRole("tab", { name: "서비스 상태" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  expect(screen.getByRole("heading", { name: "로컬 연결" })).toBeVisible();
  expect(
    screen.getByText(".venv/bin/devorchestra worker status"),
  ).toBeVisible();
  expect(screen.getByText(/GUI를 별도 설정 파일로 실행했다면/)).toBeVisible();
  expect(
    screen.getByText(
      ".venv/bin/devorchestra --config /absolute/path/to/config.yaml worker status",
    ),
  ).toBeVisible();
  await userEvent.click(screen.getByRole("tab", { name: "에이전트 모델" }));
  await userEvent.click(
    await screen.findByRole("button", { name: "모델 목록 불러오기" }),
  );
  await screen.findAllByRole("option", { name: "test-model" });
  await userEvent.selectOptions(
    screen.getByLabelText("공통 기본값 모델"),
    "test-model",
  );
  await userEvent.click(screen.getByRole("tab", { name: "웹훅 알림" }));
  expect(screen.getByRole("heading", { name: "웹훅 알림" })).toBeVisible();
  expect(
    screen.queryByRole("heading", { name: "에이전트 모델 설정" }),
  ).not.toBeInTheDocument();
  await userEvent.keyboard("{ArrowRight}");
  expect(screen.getByRole("tab", { name: "서비스 상태" })).toHaveFocus();
  expect(screen.getByRole("heading", { name: "로컬 연결" })).toBeVisible();
  expect(
    screen.getByRole("heading", { name: "실행 서비스 확인" }),
  ).toBeVisible();
  await userEvent.click(screen.getByRole("tab", { name: "에이전트 모델" }));
  expect(screen.getByLabelText("공통 기본값 모델")).toHaveValue("test-model");
  expect(save).not.toHaveBeenCalled();
});
