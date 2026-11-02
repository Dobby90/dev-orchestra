import {
  ApiError,
  authEvents,
  request,
  setCsrf,
  version,
} from "../src/api/client";
import { afterEach, describe, expect, it, vi } from "vitest";
afterEach(() => setCsrf(null));
describe("로컬 API 클라이언트", () => {
  it("쿠키와 CSRF를 같은 origin 요청에만 전달한다", async () => {
    const fetch = vi.fn().mockResolvedValue(new Response("{}"));
    vi.stubGlobal("fetch", fetch);
    setCsrf("memory-only");
    await request("/runs", { method: "POST", body: "{}" });
    expect(fetch).toHaveBeenCalledWith(
      "/api/runs",
      expect.objectContaining({
        credentials: "same-origin",
        cache: "no-store",
      }),
    );
    const h = new Headers(fetch.mock.calls[0][1].headers);
    expect(h.get("X-CSRF-Token")).toBe("memory-only");
    expect(h.get("Content-Type")).toBe("application/json");
    expect(localStorage.length).toBe(0);
  });
  it("multipart의 boundary를 브라우저에 맡긴다", async () => {
    const fetch = vi.fn().mockResolvedValue(new Response("{}"));
    vi.stubGlobal("fetch", fetch);
    await request("/specs", { method: "POST", body: new FormData() });
    expect(
      new Headers(fetch.mock.calls[0][1].headers).has("Content-Type"),
    ).toBe(false);
  });
  it("401은 인증을 무효화하고 응답의 민감한 원문을 노출하지 않는다", async () => {
    const expired = vi.fn();
    authEvents.addEventListener("expired", expired);
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            code: "SESSION_REQUIRED",
            message: "token=CANARY",
          }),
          { status: 401 },
        ),
      ),
    );
    await expect(request("/runs")).rejects.toThrow("로그인이 만료");
    expect(expired).toHaveBeenCalledTimes(1);
    authEvents.removeEventListener("expired", expired);
  });
  it("네트워크 오류를 안전한 메시지로 바꾸며 자동 재시도하지 않는다", async () => {
    const fetch = vi.fn().mockRejectedValue(new Error("CANARY"));
    vi.stubGlobal("fetch", fetch);
    await expect(request("/runs")).rejects.toThrow("로컬 API에 연결");
    expect(fetch).toHaveBeenCalledTimes(1);
  });
  it("잘못된 JSON 응답에 포함된 원문을 표시하지 않는다", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response("token=CANARY")),
    );
    await expect(request("/runs")).rejects.toMatchObject({
      code: "INVALID_RESPONSE",
    });
  });
  it("상태 충돌은 최신 조회를 요청하는 메시지다", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          new Response('{"code":"VERSION_CONFLICT"}', { status: 412 }),
        ),
    );
    await expect(request("/runs")).rejects.toMatchObject({
      status: 412,
      code: "VERSION_CONFLICT",
    });
    expect(new ApiError(412, "VERSION_CONFLICT").message).toContain("최신");
    expect(version("run", "r1", 7)).toEqual({ "If-Match": '"run:r1:v7"' });
  });
});
