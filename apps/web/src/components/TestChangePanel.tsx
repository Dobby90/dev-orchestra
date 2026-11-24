import { useQuery } from "@tanstack/react-query";
import { api } from "../api/resources";
import type { RunDetail, Schema } from "../api/types";
import { ErrorNotice, Loading } from "./common";

type ChangeRecord =
  Schema["TestChangeRecordView"] | Schema["TestChangeProposalView"];

function ChangeEvidence({ record }: { record: ChangeRecord }) {
  return (
    <div className="path">
      <ul>
        {record.changes.map((change) => (
          <li key={change.file}>
            <code>{change.file}</code>
            {change.criterion_ids.length > 0 &&
              ` · ${change.criterion_ids.join(", ")}`}
            <p>{change.reason}</p>
          </li>
        ))}
      </ul>
      {record.patch && (
        <details className="run-disclosure">
          <summary>제품·테스트 전체 패치 보기</summary>
          <pre
            className="test-change-patch"
            tabIndex={0}
            aria-label="전체 패치"
          >
            {record.patch}
          </pre>
        </details>
      )}
    </div>
  );
}

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
  const [latest, ...earlier] = [...(history ?? [])].reverse();
  return (
    <section className="panel panel-body" aria-label="기존 테스트 변경">
      <div className="test-change-heading">
        <h2>기존 테스트 변경 내역</h2>
        <span className="subtle">기록 {run.test_change_count}건</span>
      </div>
      {error && (
        <p role="alert" className="text-red path">
          {error}
        </p>
      )}
      {run.state === "waiting_approval" && !error && (proposal || latest) && (
        <p className="text-amber">
          최종 승인 전에 변경 사유와 전체 Diff를 확인해 주세요.
        </p>
      )}
      {proposal && (
        <p role="status">
          이전 버전에서 보관한 패치입니다. 재개하면 변경 근거를 검증한 뒤
          자동으로 적용하고 테스트·리뷰를 진행합니다.
        </p>
      )}
      {(proposal || latest) && (
        <details className="run-disclosure">
          <summary>변경 사유·Diff 보기</summary>
          {proposal && (
            <section className="test-change-record">
              <h3>재개 시 적용할 보관 패치</h3>
              <ChangeEvidence record={proposal} />
            </section>
          )}
          {latest && (
            <section className="test-change-record">
              <h3>최신 변경 · 파일 {latest.changes.length}개</h3>
              <ChangeEvidence record={latest} />
            </section>
          )}
          {earlier.map((record) => (
            <details className="run-disclosure" key={record.id}>
              <summary>
                이전 변경 · revision {record.revision} · 회차 {record.iteration}{" "}
                · 파일 {record.changes.length}개
              </summary>
              <ChangeEvidence record={record} />
            </details>
          ))}
        </details>
      )}
    </section>
  );
}
