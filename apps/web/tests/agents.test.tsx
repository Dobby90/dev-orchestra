import { render, screen, within, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";
import { createQueryClient } from "../src/App";
import { AgentSettingsForm } from "../src/components/AgentSettingsForm";
import { AgentUsagePanel } from "../src/components/AgentUsagePanel";
import { api } from "../src/api/resources";
import { ApiError } from "../src/api/client";

const initial = {
  version: "a".repeat(64),
  settings: { model: null, reasoning_effort: null, roles: {} },
};
const models = {
  items: [
    { model: "test-standard", reasoning_efforts: ["low", "medium", "high"] },
    { model: "test-review", reasoning_efforts: ["high", "xhigh"] },
  ],
};
function wrap(node: React.ReactNode) {
  return render(
    <QueryClientProvider client={createQueryClient()}>
      {node}
    </QueryClientProvider>,
  );
}
describe("에이전트 모델·사용량", () => {
  it("공통 설정을 상속하고 모델에서 지원하는 추론 강도만 저장한다", async () => {
    vi.spyOn(api, "agentSettings").mockResolvedValue(initial);
    const catalog = vi.spyOn(api, "codexModels").mockResolvedValue(models);
    const save = vi
      .spyOn(api, "saveAgentSettings")
      .mockImplementation(async (body) => ({
        version: "b".repeat(64),
        settings: body.settings,
      }));
    wrap(<AgentSettingsForm />);
    const common = await screen.findByRole("combobox", {
      name: "공통 기본값 모델",
    });
    expect(common).toBeDisabled();
    expect(catalog).not.toHaveBeenCalled();
    await userEvent.click(
      screen.getByRole("button", { name: "모델 목록 불러오기" }),
    );
    await waitFor(() => expect(common).toBeEnabled());
    await userEvent.selectOptions(common, "test-standard");
    await userEvent.selectOptions(
      screen.getByRole("combobox", { name: "공통 기본값 추론 강도" }),
      "low",
    );
    await userEvent.selectOptions(
      screen.getByRole("combobox", { name: "Review 모델" }),
      "test-review",
    );
    const effort = screen.getByRole("combobox", { name: "Review 추론 강도" });
    expect(within(effort).queryByRole("option", { name: "low" })).toBeNull();
    const button = screen.getByRole("button", { name: "에이전트 설정 저장" });
    expect(button).toBeDisabled();
    await userEvent.selectOptions(effort, "xhigh");
    expect(button).toBeEnabled();
    await userEvent.click(button);
    await waitFor(() =>
      expect(save).toHaveBeenCalledWith(
        {
          expected_version: initial.version,
          settings: {
            model: "test-standard",
            reasoning_effort: "low",
            roles: {
              reviewer: { model: "test-review", reasoning_effort: "xhigh" },
            },
          },
        },
        expect.anything(),
      ),
    );
    expect(
      await screen.findByText("에이전트 설정을 저장했습니다."),
    ).toBeVisible();
  });

  it("충돌 시 편집 내용을 보존하고 최신 설정을 명시적으로 다시 불러온다", async () => {
    const read = vi.spyOn(api, "agentSettings").mockResolvedValue(initial);
    vi.spyOn(api, "codexModels").mockResolvedValue(models);
    vi.spyOn(api, "saveAgentSettings").mockRejectedValue(
      new ApiError(412, "VERSION_CONFLICT"),
    );
    wrap(<AgentSettingsForm />);
    await screen.findByRole("combobox", { name: "공통 기본값 모델" });
    await userEvent.click(
      screen.getByRole("button", { name: "모델 목록 불러오기" }),
    );
    const model = screen.getByRole("combobox", { name: "공통 기본값 모델" });
    await waitFor(() => expect(model).toBeEnabled());
    await userEvent.selectOptions(model, "test-review");
    await userEvent.click(
      screen.getByRole("button", { name: "에이전트 설정 저장" }),
    );
    expect(await screen.findByText(/다른 작업으로 상태가 변경/)).toBeVisible();
    expect(model).toHaveValue("test-review");
    read.mockResolvedValue({ ...initial, version: "c".repeat(64) });
    await userEvent.click(
      screen.getByRole("button", { name: "저장된 설정 다시 불러오기" }),
    );
    await waitFor(() =>
      expect(
        screen.getByRole("combobox", { name: "공통 기본값 모델" }),
      ).toHaveValue(""),
    );
  });

  it("부분 집계와 미확인·실제 0 토큰을 구분한다", async () => {
    vi.spyOn(api, "agentUsage").mockResolvedValue({
      agents: { developer: { model: "saved-model", reasoning_effort: "high" } },
      usage_status: "partial",
      measured_attempts: 1,
      total_attempts: 2,
      usage: { input_tokens: 0, cached_input_tokens: 0, output_tokens: 0 },
      attempts: [
        {
          step_id: 1,
          role: "developer",
          revision: 0,
          iteration: 0,
          attempt: 1,
          status: "interrupted",
          completed_turns: 0,
          usage: null,
          usage_status: "unknown",
        },
        {
          step_id: 2,
          role: "developer",
          revision: 0,
          iteration: 0,
          attempt: 2,
          status: "completed",
          completed_turns: 1,
          usage: { input_tokens: 0, cached_input_tokens: 0, output_tokens: 0 },
          usage_status: "reported",
          duration_seconds: 1.2,
        },
      ],
    });
    wrap(<AgentUsagePanel rid="r1" />);
    expect(await screen.findByText("saved-model")).toBeVisible();
    expect(screen.getByText("일부 집계")).toBeVisible();
    expect(screen.getByText(/수집된 시도 1 \/ 전체 2/)).toBeVisible();
    expect(screen.getAllByText(/입력 0 · 캐시 입력 0 · 출력 0/)).toHaveLength(
      2,
    );
    expect(screen.getAllByText("미확인").length).toBeGreaterThan(0);
    expect(screen.getByText("1.2초")).toBeVisible();
  });
});
