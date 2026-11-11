import { useState } from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { expect, it, vi } from "vitest";
import { api } from "../src/api/resources";
import type { Schema } from "../src/api/types";
import { RunAgentSettings } from "../src/components/RunAgentSettings";

it("기본 설정을 복사하여 역할별로 변경하고 기본 설정 사용으로 돌아간다", async () => {
  vi.spyOn(api, "agentSettings").mockResolvedValue({
    version: "v",
    settings: { model: "test-standard", reasoning_effort: "medium", roles: {} },
  });
  vi.spyOn(api, "codexModels").mockResolvedValue({
    items: [
      { model: "test-standard", reasoning_efforts: ["medium"] },
      { model: "test-review", reasoning_efforts: ["high"] },
    ],
  });
  const save = vi.spyOn(api, "saveAgentSettings");
  function Harness() {
    const [value, setValue] = useState<Schema["AgentSettings"] | null>(null);
    return (
      <>
        <RunAgentSettings value={value} onChange={setValue} />
        <output data-testid="settings">{JSON.stringify(value)}</output>
      </>
    );
  }
  render(
    <QueryClientProvider client={new QueryClient()}>
      <Harness />
    </QueryClientProvider>,
  );
  await userEvent.click(screen.getByRole("button", { name: "이 작업만 변경" }));
  await screen.findByLabelText("공통 기본값 모델");
  await userEvent.click(
    screen.getByRole("button", { name: "모델 목록 불러오기" }),
  );
  const model = screen.getByLabelText("Review 모델");
  await waitFor(() => expect(model).toBeEnabled());
  await userEvent.selectOptions(model, "test-review");
  await userEvent.selectOptions(
    screen.getByLabelText("Review 추론 강도"),
    "high",
  );
  expect(
    JSON.parse(screen.getByTestId("settings").textContent!).roles.reviewer,
  ).toEqual({ model: "test-review", reasoning_effort: "high" });
  expect(save).not.toHaveBeenCalled();
  await userEvent.click(screen.getByRole("button", { name: "기본 설정 사용" }));
  expect(screen.getByTestId("settings")).toHaveTextContent("null");
});
