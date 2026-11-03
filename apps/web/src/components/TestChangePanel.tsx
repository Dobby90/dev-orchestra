import { useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/resources";
import { ApiError } from "../api/client";
import type { RunDetail, Schema } from "../api/types";
import { ErrorNotice, Loading } from "./common";
import { Button } from "./ui/button";
import { Modal } from "./ui/dialog";

type Decision = {
  approve: boolean;
  proposal: Schema["TestChangeProposalView"];
  version: number;
  key: string;
};

export function TestChangePanel({ run }: { run: RunDetail }) {
  const client = useQueryClient();
  const [decision, setDecision] = useState<Decision | null>(null);
  const [reason, setReason] = useState("");
  const query = useQuery({
    queryKey: ["test-changes", run.id, run.version],
    queryFn: () => api.testChanges(run.id),
    retry: false,
  });
  const control = useMutation({
    mutationFn: (target: Decision) =>
      api.control(
        run.id,
        target.version,
        target.approve ? "approve-test-changes" : "reject-test-changes",
        {
          proposal_hash: target.proposal.proposal_hash,
          input_sha: target.proposal.input_sha,
          reason: target.approve ? "" : reason,
        },
        target.key,
      ),
    onSuccess: () => {
      setDecision(null);
      for (const key of ["run", "steps", "test-changes", "report"])
        client.invalidateQueries({ queryKey: [key, run.id] });
      client.invalidateQueries({ queryKey: ["runs"] });
      client.invalidateQueries({ queryKey: ["dashboard"] });
    },
    onError: () => client.invalidateQueries({ queryKey: ["run", run.id] }),
  });
  const uncertain =
    control.error instanceof ApiError &&
    (control.error.status === 0 || control.error.status >= 500);
  const stale = !!decision && decision.version !== run.version;
  function confirm(event: FormEvent) {
    event.preventDefault();
    if (
      decision &&
      !control.isPending &&
      (!stale || uncertain) &&
      (decision.approve || reason.trim())
    )
      control.mutate(decision);
  }
  if (query.isPending) return <Loading />;
  if (query.error)
    return <ErrorNotice error={query.error} retry={() => query.refetch()} />;
  const proposal = query.data.proposal;
  if (!proposal) return null;
  function open(approve: boolean) {
    if (!proposal) return;
    control.reset();
    setReason("");
    setDecision({
      approve,
      proposal,
      version: run.version,
      key: crypto.randomUUID(),
    });
  }
  return (
    <section className="panel" aria-label="기존 테스트 변경">
      <h2>기존 테스트 변경 검토</h2>
      <p>
        요구사항에 따른 기존 테스트 변경이 포함되어 전체 패치를 보관했습니다.
        변경 사유와 제품·테스트 diff를 확인해 주세요. 승인 후 전체 테스트와 독립
        리뷰를 수행합니다.
      </p>
      <ul>
        {proposal.changes.map((change) => (
          <li key={change.file}>
            <code>{change.file}</code> · {change.criterion_ids.join(", ")}
            <p>{change.reason}</p>
          </li>
        ))}
      </ul>
      {query.data.error && <p role="alert">{query.data.error}</p>}
      <details>
        <summary>승인 대상 전체 패치 보기</summary>
        <pre className="test-change-patch">{proposal.patch}</pre>
      </details>
      {proposal.approved && !query.data.error && (
        <p role="status">승인된 패치를 적용하고 검증할 준비가 되었습니다.</p>
      )}
      <div className="inline-actions">
        {!query.data.error &&
          run.allowed_actions?.includes("approve-test-changes") && (
            <Button onClick={() => open(true)}>테스트 변경 승인</Button>
          )}
        {run.allowed_actions?.includes("reject-test-changes") && (
          <Button variant="secondary" onClick={() => open(false)}>
            반려·다시 작성
          </Button>
        )}
      </div>
      <Modal
        open={!!decision}
        onOpenChange={(value) => {
          if (!value && !control.isPending) setDecision(null);
        }}
        title={decision?.approve ? "테스트 변경 승인" : "테스트 변경 반려"}
        description={
          decision?.approve
            ? "아래의 정확한 패치 전체를 적용하고 실행을 재개합니다. 최종 승인은 검증 후 별도로 진행합니다."
            : "패치를 적용하지 않고 사유를 전달해 해당 단계에서 다시 작성합니다."
        }
      >
        <form onSubmit={confirm}>
          <details open>
            <summary>검토한 전체 패치</summary>
            <pre className="test-change-patch">{decision?.proposal.patch}</pre>
          </details>
          {!decision?.approve && (
            <label>
              반려 사유·수정 지시
              <textarea
                value={reason}
                onChange={(event) => setReason(event.target.value)}
                required
                maxLength={4000}
                disabled={control.isPending || uncertain}
              />
            </label>
          )}
          {stale && !uncertain && (
            <p role="alert">
              작업 상태가 변경되었습니다. 닫고 최신 제안을 확인해 주세요.
            </p>
          )}
          {control.error && <ErrorNotice error={control.error} />}
          <Button
            type="submit"
            disabled={control.isPending || (stale && !uncertain)}
          >
            {control.isPending
              ? "처리 중…"
              : uncertain
                ? "같은 요청 재확인"
                : decision?.approve
                  ? "승인하고 재개"
                  : "반려하고 다시 작성"}
          </Button>
        </form>
      </Modal>
    </section>
  );
}
