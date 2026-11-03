import { useQuery } from "@tanstack/react-query";
import { api } from "../api/resources";
import type { RunDetail } from "../api/types";
import { ErrorNotice, Loading } from "./common";

export function TestChangePanel({ run }: { run: RunDetail }) {
  const query = useQuery({
    queryKey: ["test-changes", run.id, run.version],
    queryFn: () => api.testChanges(run.id),
    retry: false,
  });
  if (query.isPending) return <Loading />;
  if (query.error)
    return <ErrorNotice error={query.error} retry={() => query.refetch()} />;
  const { proposal, history, error } = query.data;
  return (
    <section className="panel panel-body" aria-label="기존 테스트 변경">
      <h2>기존 테스트 변경 내역</h2>
      <p>
        요구사항에 따른 기존 테스트 수정은 중간 승인 없이 자동으로 적용하고
        테스트·독립 리뷰로 검증합니다. 최종 승인 전에 변경 사유와 전체 diff를
        확인해 주세요.
      </p>
      {error && <p role="alert">{error}</p>}
      {proposal && (
        <p role="status">
          이전 버전에서 보관한 패치입니다. 재개하면 변경 근거를 검증한 뒤
          자동으로 적용하고 테스트·리뷰를 진행합니다.
        </p>
      )}
      {[...(proposal ? [proposal] : []), ...(history ?? [])].map((record) => (
        <div key={record.id}>
          <ul>
            {record.changes.map((change) => (
              <li key={change.file}>
                <code>{change.file}</code> · {change.criterion_ids.join(", ")}
                <p>{change.reason}</p>
              </li>
            ))}
          </ul>
          <details>
            <summary>제품·테스트 전체 패치 보기</summary>
            <pre className="test-change-patch">{record.patch}</pre>
          </details>
        </div>
      ))}
    </section>
  );
}
