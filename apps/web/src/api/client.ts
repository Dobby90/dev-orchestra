export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
  ) {
    super(
      messages[code] ??
        (status === 404
          ? "요청한 항목을 찾을 수 없습니다."
          : "요청을 처리하지 못했습니다. 현재 상태를 확인하고 다시 시도해 주세요."),
    );
  }
}
const messages: Record<string, string> = {
  NETWORK:
    "로컬 API에 연결할 수 없습니다. API 서버를 확인한 뒤 다시 시도해 주세요.",
  SESSION_REQUIRED: "로그인이 만료되었습니다. 새 로그인 코드를 입력해 주세요.",
  LOCAL_RATE_LIMIT:
    "로그인 시도가 많습니다. 잠시 후 새 코드로 다시 시도해 주세요.",
  VERSION_CONFLICT:
    "다른 작업으로 상태가 변경되었습니다. 최신 내용을 확인하고 다시 시도해 주세요.",
  PREVIEW_STALE:
    "미리보기의 유효 시간이 지났거나 프로젝트가 변경되었습니다. 다시 미리보기 해주세요.",
  CSRF_INVALID: "세션을 다시 확인해야 합니다. 새로고침 후 다시 시도해 주세요.",
  PROJECT_REQUIRED: "등록되지 않은 CLI 작업입니다. CLI에서 제어해 주세요.",
  VALIDATION_ERROR: "입력 형식이나 프로젝트 정책을 확인해 주세요.",
  BRANCH_CONFLICT:
    "같은 이름의 Feature 브랜치가 있습니다. 새 브랜치 이름을 입력해 주세요.",
};
let csrf: string | null = null;
export const authEvents = new EventTarget();
export function setCsrf(value: string | null) {
  csrf = value;
}
export async function request<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const headers = new Headers(init.headers);
  const method = init.method ?? "GET";
  if (!["GET", "HEAD"].includes(method)) {
    if (!(init.body instanceof FormData))
      headers.set("Content-Type", "application/json");
    if (csrf) headers.set("X-CSRF-Token", csrf);
  }
  let response: Response;
  try {
    response = await fetch(`/api${path}`, {
      ...init,
      method,
      headers,
      credentials: "same-origin",
      cache: "no-store",
      signal: init.signal ?? AbortSignal.timeout(30000),
    });
  } catch {
    throw new ApiError(0, "NETWORK");
  }
  if (!response.ok) {
    const data: unknown = await response.json().catch(() => null);
    const code =
      typeof data === "object" &&
      data &&
      "code" in data &&
      typeof data.code === "string"
        ? data.code
        : "REQUEST_REJECTED";
    if (response.status === 401) {
      csrf = null;
      authEvents.dispatchEvent(new Event("expired"));
    }
    throw new ApiError(response.status, code);
  }
  if (response.status === 204) return undefined as T;
  try {
    return (await response.json()) as T;
  } catch {
    throw new ApiError(502, "INVALID_RESPONSE");
  }
}
export function version(prefix: "run" | "project", id: string, value: number) {
  return { "If-Match": `"${prefix}:${id}:v${value}"` };
}
