import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/resources";
import type { RunDetail } from "../api/types";
import { ErrorNotice, Loading } from "./common";
import { Button } from "./ui/button";

export function RunPushPanel({
  run,
  onCleanup,
}: {
  run: RunDetail;
  onCleanup: () => void;
}) {
  const client = useQueryClient();
  const query = useQuery({
    queryKey: ["push", run.id, run.version],
    queryFn: () => api.pushPreview(run.id),
    refetchInterval: (q) =>
      q.state.data?.result?.status === "running" ? 1000 : false,
  });
  const push = useMutation({
    mutationFn: () =>
      api.pushBranch(run.id, run.version, {
        settings_version: query.data!.settings_version,
        remote_url: query.data!.remote_url!,
        branch: query.data!.branch,
        approved_sha: query.data!.approved_sha!,
      }),
    onSuccess: (value) =>
      client.setQueryData(["push", run.id, run.version], value),
    onSettled: () => {
      client.invalidateQueries({ queryKey: ["run", run.id] });
      client.invalidateQueries({ queryKey: ["push", run.id] });
    },
  });
  if (query.isPending) return <Loading />;
  if (query.error)
    return <ErrorNotice error={query.error} retry={() => query.refetch()} />;
  const p = query.data;
  const success =
    p.result?.status === "success" &&
    p.result.remote_url === p.remote_url &&
    p.result.branch === p.branch &&
    p.result.sha === p.approved_sha;
  return (
    <section className="panel panel-body" aria-label="작업 브랜치 Push">
      <h2>작업 브랜치 Push</h2>
      <p>최종 승인 → 완료 → 사용자가 Push → 사용자가 Worktree 정리</p>
      <p className="mono path">
        원격 저장소: {p.remote || "프로젝트 Git 설정 필요"} ·{" "}
        {p.remote_url ?? "주소 확인 필요"}
      </p>
      <p className="mono path">대상 작업 브랜치: {p.branch}</p>
      <p className="mono path">최종 승인 SHA: {p.approved_sha}</p>
      <p className="help">
        버튼을 누르면 승인 SHA와 현재 작업 브랜치를 다시 확인하고 해당 브랜치만
        Push합니다.
      </p>
      <Button
        disabled={
          push.isPending || !p.remote_url || run.handoff?.status !== "matching"
        }
        onClick={() => push.mutate()}
      >
        {push.isPending
          ? "Push 진행 중…"
          : success
            ? "작업 브랜치 Push 재확인"
            : p.result
              ? "작업 브랜치 Push 재시도"
              : "작업 브랜치 Push"}
      </Button>
      {push.error && (
        <ErrorNotice error={push.error} retry={() => query.refetch()} />
      )}
      <div role="status">
        {push.isPending
          ? "Push 진행 중입니다."
          : p.result && (
              <p>
                {p.result.status === "running"
                  ? "이전 Push 결과가 확인되지 않았습니다. 재시도하면 원격 상태를 확인합니다."
                  : success
                    ? "작업 브랜치 Push에 성공했습니다."
                    : p.result.status === "success"
                      ? "이전 대상 Push는 성공했습니다. 변경된 원격 대상을 확인한 뒤 다시 Push하세요."
                      : p.result.message}
              </p>
            )}
      </div>
      {p.result && (
        <p className="help">
          마지막 Push 시도: {p.result.checked_at} · {p.result.remote_url} ·{" "}
          {p.result.branch} · {p.result.sha}
        </p>
      )}
      {success && (
        <div>
          <p>
            다음 단계: Worktree 정리. Push 결과와 정리 결과는 별도로 보존됩니다.
          </p>
          {run.execution?.worktree_removed ? (
            <p>Worktree 정리가 완료되었습니다. 브랜치와 이력은 보존됩니다.</p>
          ) : (
            <Button
              variant="secondary"
              disabled={push.isPending}
              onClick={onCleanup}
            >
              Worktree 정리
            </Button>
          )}
        </div>
      )}
    </section>
  );
}
