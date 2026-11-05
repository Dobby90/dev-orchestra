import type { RunDetail } from "../api/types";

export function RunHandoffPanel({ run }: { run: RunDetail }) {
  const handoff = run.handoff;
  const capacity = run.capacity;
  const progress = run.workspace_progress;
  const waits = {
    total_capacity: "전체 실행 용량 대기",
    repository_capacity: "동일 저장소 실행 용량 대기",
    shared_resource: "공유 테스트 자원 대기",
    unknown: "대기 상태 확인 필요",
  };
  return (
    <section className="panel form-grid" aria-label="작업 인계와 실행 상태">
      {capacity && (
        <p role="status">
          동시 실행 {capacity.active} / {capacity.total} · 동일 저장소{" "}
          {capacity.repository_active} / {capacity.repository_limit}
          {capacity.wait_reason && ` · ${waits[capacity.wait_reason]}`}
        </p>
      )}
      {progress && (
        <div>
          <strong>보존된 역할 작업</strong>
          <p>
            중간 커밋 {progress.commits?.length ?? 0}개 · 보존된 변경 파일{" "}
            {progress.preserved_changes}개
          </p>
          <p className="mono path">
            복구 지점: {progress.checkpoint_sha ?? "확인 필요"}
          </p>
          <p>
            {progress.recovery === "metadata"
              ? "편집·커밋·테스트를 반복하지 않고 결과 요약만 보완합니다."
              : progress.recovery === "same_role"
                ? "답변 없이 재개하면 소유권과 파일을 검증한 뒤 같은 역할·시도를 이어갑니다. 미완료 변경과 새 요구사항을 자동 병합하지 않습니다."
                : "복구 근거를 확인할 수 없습니다. 파일을 보존하고 상태를 확인하세요."}
          </p>
        </div>
      )}
      <p className="help">
        선택한 테스트:{" "}
        {run.test_scope === "affected" ? "변경 영향 범위" : "전체"} · 실제 실행
        범위와 확대 사유는 테스트 결과에서 확인할 수 있습니다. 리뷰는 Run의 누적
        변경·직접 영향을 받는 코드와 기존 미해결 이슈를 확인합니다.
      </p>
      {handoff && ["waiting_approval", "completed"].includes(run.state) && (
        <div>
          <h2>로컬 작업 브랜치 인계</h2>
          <p className="mono path">원본 프로젝트: {handoff.repository}</p>
          <p className="mono path">로컬 브랜치: {handoff.branch}</p>
          <p className="mono path">
            최종 승인 SHA: {handoff.approved_sha ?? "승인 전"}
          </p>
          <p className="mono path">
            현재 브랜치 SHA: {handoff.current_branch_sha ?? "확인 불가"}
          </p>
          {handoff.status === "changed" && (
            <p role="alert" className="notice">
              현재 브랜치 또는 저장소 연결이 승인 후보와 다릅니다. 과거 승인
              SHA와 현재 변경을 구분해 확인하세요.
            </p>
          )}
          <pre>{handoff.commands.join("\n")}</pre>
          <p className="help">{handoff.notice}</p>
        </div>
      )}
    </section>
  );
}
