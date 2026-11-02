import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";
import { appendBounded, decodeEvent, useRunEvents } from "../src/hooks/events";
import { createQueryClient } from "../src/App";
const event = (id: number, kind = "log.appended") => ({
  schema_version: 1 as const,
  id,
  run_id: "r1",
  kind,
  occurred_at: "2026-11-02",
  data: { text: "한글\n", source: "worker.log", role: null, level: "unknown" },
});
class Stream {
  static instances: Stream[] = [];
  listeners = new Map<string, EventListener>();
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;
  constructor(public url: string) {
    Stream.instances.push(this);
  }
  addEventListener(kind: string, listener: EventListener) {
    this.listeners.set(kind, listener);
  }
  close() {
    this.closed = true;
  }
  emit(value: ReturnType<typeof event>) {
    this.listeners.get(value.kind)?.(
      new MessageEvent(value.kind, { data: JSON.stringify(value) }),
    );
  }
}
describe("영속 이벤트와 bounded 로그", () => {
  it("다른 Run·손상된 envelope·잘못된 ID를 거부한다", () => {
    expect(decodeEvent(event(1), "r1")?.id).toBe(1);
    expect(decodeEvent(event(1), "other")).toBeNull();
    expect(decodeEvent({ ...event(1), data: [] }, "r1")).toBeNull();
    expect(decodeEvent(event(-1), "r1")).toBeNull();
  });
  it("로그 수와 UTF-8 바이트 예산을 모두 제한한다", () => {
    const item = {
      id: 1,
      text: "한".repeat(2000),
      role: null,
      level: "unknown",
      source: "s",
      step: null,
    };
    const result = appendBounded(
      [],
      Array.from({ length: 1100 }, (_, id) => ({ ...item, id })),
    );
    expect(result.items.length).toBeLessThanOrEqual(1000);
    expect(
      result.items.reduce(
        (n, l) => n + new TextEncoder().encode(l.text).length,
        0,
      ),
    ).toBeLessThanOrEqual(1048576);
    expect(result.items.at(-1)?.id).toBe(1099);
    expect(result.removed).toBeGreaterThan(0);
  });
  it("SSE 단절 동안 영속 이력을 조회하고 연결을 재생성하지 않는다", async () => {
    vi.useFakeTimers();
    Stream.instances = [];
    vi.stubGlobal("EventSource", Stream);
    const { api } = await import("../src/api/resources");
    vi.spyOn(api, "session").mockResolvedValue({
      authenticated: true,
      csrf_token: "csrf",
      expires_at: "2026-11-03",
    });
    const history = vi.spyOn(api, "history").mockResolvedValue({
      items: [event(4)],
      has_more: false,
      next_cursor: 4,
    });
    const client = createQueryClient();
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
    const { result, unmount } = renderHook(() => useRunEvents("r1"), {
      wrapper,
    });
    try {
      act(() => Stream.instances.at(-1)!.onerror?.());
      await act(async () => {
        await vi.advanceTimersByTimeAsync(3300);
      });
      expect(history).toHaveBeenCalledWith("r1", 0);
      expect(result.current.logs[0]?.id).toBe(4);
      expect(result.current.connection).toBe("reconnecting");
      expect(Stream.instances.length).toBe(1);
    } finally {
      unmount();
      vi.useRealTimers();
    }
  });
  it("중복·역순 이벤트를 무시하고 구독 종료 시 Worker 제어 요청을 보내지 않는다", async () => {
    Stream.instances = [];
    vi.stubGlobal("EventSource", Stream);
    const fetch = vi.fn();
    vi.stubGlobal("fetch", fetch);
    const client = createQueryClient();
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
    const { result, unmount } = renderHook(() => useRunEvents("r1"), {
      wrapper,
    });
    const stream = Stream.instances.at(-1)!;
    act(() => {
      stream.onopen?.();
      stream.emit(event(2));
      stream.emit(event(2));
      stream.emit(event(1));
    });
    await waitFor(() => expect(result.current.logs.length).toBe(1));
    expect(result.current.connection).toBe("live");
    unmount();
    expect(stream.closed).toBe(true);
    expect(fetch).not.toHaveBeenCalled();
  });
});
