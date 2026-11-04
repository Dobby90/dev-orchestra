import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { api } from "../api/resources";
import { ApiError } from "../api/client";
import type { RunDetail } from "../api/types";
import { ErrorNotice } from "./common";
import { Button } from "./ui/button";
import { Modal } from "./ui/dialog";

export function DeleteRunButton({ run }: { run: RunDetail }) {
  const client = useQueryClient();
  const navigate = useNavigate();
  const [target, setTarget] = useState<RunDetail | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [attempted, setAttempted] = useState(false);

  function finish() {
    client.removeQueries({ predicate: (q) => q.queryKey.includes(run.id) });
    client.invalidateQueries({ queryKey: ["runs"] });
    client.invalidateQueries({ queryKey: ["dashboard"] });
    navigate("/", { replace: true });
  }
  async function open() {
    setBusy(true);
    setError(null);
    try {
      const fresh = await api.run(run.id);
      client.setQueryData(["run", run.id], fresh);
      setTarget(fresh);
    } catch (failure) {
      if (attempted && failure instanceof ApiError && failure.status === 404)
        finish();
      else setError(failure);
    } finally {
      setBusy(false);
    }
  }
  async function remove() {
    if (!target || busy) return;
    setBusy(true);
    setError(null);
    setAttempted(true);
    try {
      await api.deleteRun(run.id, target.version);
      finish();
    } catch (failure) {
      setError(failure);
      setTarget(null);
      client.invalidateQueries({ queryKey: ["run", run.id] });
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <Button variant="danger" disabled={busy} onClick={open}>
        {run.deletion_pending ? "기록 삭제 재시도" : "작업 기록 삭제"}
      </Button>
      {error && <ErrorNotice error={error} />}
      <Modal
        open={!!target}
        onOpenChange={(value) => {
          if (!value && !busy) setTarget(null);
        }}
        title="작업 기록 영구 삭제"
        description="삭제한 작업 기록은 복원할 수 없습니다."
      >
        <p className="notice">
          이 작업의 실행 단계·요구사항·로그·보고서·테스트 및 리뷰 결과·승인
          근거·테스트 결과 파일·격리된 Worktree와 로컬 작업 브랜치를 영구
          삭제합니다. 원본 작업 디렉터리와 원격 브랜치는 보존합니다. 테스트 결과
          파일 외 미커밋 변경이 있거나 작업 브랜치에 추가된 커밋이 있으면
          삭제하지 않습니다. 파일 삭제가 중단되면 일부 파일이 이미 삭제되었을 수
          있으며, 기록 삭제를 재시도할 수 있습니다.
        </p>
        <p className="mono">{run.id}</p>
        <p className="mono">삭제할 로컬 브랜치: {target?.branch}</p>
        <Button
          variant="danger"
          disabled={
            busy ||
            !target ||
            !["completed", "cancelled"].includes(target.state)
          }
          onClick={remove}
        >
          {busy ? "삭제 중…" : "영구 삭제 확인"}
        </Button>
      </Modal>
    </>
  );
}
