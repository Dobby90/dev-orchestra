import { useQuery } from "@tanstack/react-query";
import { api } from "../api/resources";
import type { Schema } from "../api/types";
import { ErrorNotice, Loading, roles } from "./common";

const labels = {
  reported: "집계 완료",
  partial: "일부 집계",
  unknown: "미확인",
};

function formatDuration(seconds: number | null | undefined) {
  if (seconds == null) return "미확인";
  const tenths = Math.round(seconds * 10);
  const hours = Math.floor(tenths / 36000);
  const minutes = Math.floor((tenths % 36000) / 600);
  const remainingSeconds = (tenths % 600) / 10;
  if (hours) return `${hours}시간 ${minutes}분 ${remainingSeconds}초`;
  if (minutes) return `${minutes}분 ${remainingSeconds}초`;
  return `${remainingSeconds}초`;
}

export function TokenCounts({
  usage,
}: {
  usage: Schema["TokenUsage"] | null | undefined;
}) {
  return usage ? (
    <>
      입력 {usage.input_tokens.toLocaleString("ko-KR")} · 캐시 입력{" "}
      {usage.cached_input_tokens.toLocaleString("ko-KR")} · 출력{" "}
      {usage.output_tokens.toLocaleString("ko-KR")}
    </>
  ) : (
    <>미확인</>
  );
}
export function AgentUsagePanel({ rid }: { rid: string }) {
  const query = useQuery({
    queryKey: ["agent-usage", rid],
    queryFn: () => api.agentUsage(rid),
    refetchInterval: 3000,
  });
  if (query.isPending) return <Loading />;
  if (query.error)
    return <ErrorNotice error={query.error} retry={() => query.refetch()} />;
  const data = query.data;
  return (
    <section className="panel-body" aria-label="에이전트 모델 및 사용량">
      <h2>실행에 저장된 모델 설정</h2>
      <div
        className="table-scroll"
        tabIndex={0}
        role="region"
        aria-label="역할별 모델 설정"
      >
        <table>
          <thead>
            <tr>
              <th>에이전트</th>
              <th>설정 모델</th>
              <th>추론 강도</th>
            </tr>
          </thead>
          <tbody>
            {Object.entries(data.agents).map(([role, options]) => (
              <tr key={role}>
                <td>{roles[role] ?? role}</td>
                <td>{options.model ?? "Codex 기본값 사용"}</td>
                <td>{options.reasoning_effort ?? "Codex 기본값 사용"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <h2>Codex 토큰 사용량</h2>
      <p>
        <strong>{labels[data.usage_status]}</strong> ·{" "}
        <TokenCounts usage={data.usage} />
      </p>
      <p className="help">
        수집된 시도 {data.measured_attempts} / 전체 {data.total_attempts} · 캐시
        입력은 입력에 포함됩니다. 실패·재시도도 합계에 포함하며, 중단되거나
        기록이 없는 사용량은 미확인으로 표시합니다. 계정 전체의 잔여 한도와는
        별개입니다.
      </p>
      <div
        className="table-scroll"
        tabIndex={0}
        role="region"
        aria-label="시도별 토큰 사용량"
      >
        <table>
          <thead>
            <tr>
              <th>에이전트 · 시도</th>
              <th>설정 모델 · 추론</th>
              <th>토큰 사용량</th>
              <th>Codex 소요 시간</th>
            </tr>
          </thead>
          <tbody>
            {data.attempts.map((attempt) => (
              <tr key={attempt.step_id}>
                <td>
                  {roles[attempt.role] ?? attempt.role}
                  <br />
                  <span className="help">
                    revision {attempt.revision} · 회차 {attempt.iteration} ·
                    시도 {attempt.attempt} · {attempt.status}
                  </span>
                </td>
                <td>
                  {attempt.model ?? "Codex 기본값"} ·{" "}
                  {attempt.reasoning_effort ?? "기본값"}
                </td>
                <td>
                  {labels[attempt.usage_status]}
                  <br />
                  <TokenCounts usage={attempt.usage} />
                </td>
                <td>{formatDuration(attempt.duration_seconds)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {!data.attempts.length && (
        <p className="subtle">아직 실행한 에이전트가 없습니다.</p>
      )}
    </section>
  );
}
