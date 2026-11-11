import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../src/api/resources";
import type { Schema } from "../src/api/types";
import { WebhookSettingsForm } from "../src/components/WebhookSettingsForm";

const initial: Schema["WebhookSettingsView"] = {
  version: "0".repeat(32),
  enabled: false,
  app: "mattermost",
  url_configured: false,
  apps: [{ id: "mattermost", label: "Mattermost" }],
  last_delivery: null,
};

function mount(value = initial) {
  vi.spyOn(api, "webhookSettings").mockResolvedValue(value);
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <WebhookSettingsForm />
    </QueryClientProvider>,
  );
  return client;
}

afterEach(() => vi.restoreAllMocks());

describe("웹훅 알림 설정", () => {
  it("앱과 URL을 저장하고 URL을 지운 뒤 테스트 발송을 제공한다", async () => {
    const value = {
      ...initial,
      version: "1".repeat(32),
      enabled: true,
      url_configured: true,
    };
    const save = vi.spyOn(api, "saveWebhookSettings").mockResolvedValue(value);
    const test = vi.spyOn(api, "testWebhook").mockResolvedValue({
      ...value,
      last_delivery: {
        status: "sent",
        message: "웹훅을 발송했습니다.",
        occurred_at: "2026-11-11T00:00:00Z",
        attempts: 1,
        event_id: null,
      },
    });
    const client = mount();
    expect(await screen.findByLabelText("알림 앱")).toHaveValue("mattermost");
    expect(screen.getByRole("button", { name: "테스트 발송" })).toBeDisabled();
    fireEvent.click(screen.getByLabelText("웹훅 알림 사용"));
    expect(
      screen.getByRole("button", { name: "웹훅 설정 저장" }),
    ).toBeDisabled();
    const input = screen.getByLabelText("웹훅 URL");
    expect(input).toHaveAttribute("type", "password");
    fireEvent.change(input, {
      target: { value: "https://mattermost.example/hooks/test-key" },
    });
    fireEvent.click(screen.getByRole("button", { name: "웹훅 설정 저장" }));
    await screen.findByText("웹훅 설정을 저장했습니다.");
    expect(save).toHaveBeenCalledWith({
      expected_version: initial.version,
      enabled: true,
      app: "mattermost",
      url: "https://mattermost.example/hooks/test-key",
    });
    expect(screen.getByLabelText("웹훅 URL")).toHaveValue("");
    expect(
      JSON.stringify(client.getQueryData(["webhook-settings"])),
    ).not.toContain("test-key");
    fireEvent.click(screen.getByRole("button", { name: "테스트 발송" }));
    await screen.findByText(/웹훅을 발송했습니다/);
    expect(test).toHaveBeenCalledWith(value.version);
    client.clear();
  });

  it("URL을 다시 입력하지 않고 비활성화하며 편집 중에는 테스트를 막는다", async () => {
    const save = vi
      .spyOn(api, "saveWebhookSettings")
      .mockResolvedValue(initial);
    const value = { ...initial, enabled: true, url_configured: true };
    const client = mount(value);
    fireEvent.click(await screen.findByLabelText("웹훅 알림 사용"));
    expect(screen.getByRole("button", { name: "테스트 발송" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "웹훅 설정 저장" }));
    await waitFor(() =>
      expect(save).toHaveBeenCalledWith({
        expected_version: value.version,
        app: "mattermost",
        enabled: false,
        url: null,
      }),
    );
    client.clear();
  });

  it("테스트 발송 실패를 표시하고 기존 URL과 비밀값을 노출하지 않는다", async () => {
    const value = { ...initial, url_configured: true };
    vi.spyOn(api, "testWebhook").mockResolvedValue({
      ...value,
      last_delivery: {
        status: "failed",
        message: "웹훅 연결에 실패했습니다.",
        occurred_at: "2026-11-11T00:00:00Z",
        attempts: 1,
        event_id: null,
      },
    });
    const client = mount(value);
    fireEvent.click(await screen.findByRole("button", { name: "테스트 발송" }));
    await screen.findByText(/웹훅 연결에 실패했습니다/);
    expect(screen.getByLabelText("웹훅 URL")).toHaveValue("");
    client.clear();
  });
});
