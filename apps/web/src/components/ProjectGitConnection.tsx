import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../api/resources";
import { Button } from "./ui/button";
import { ErrorNotice } from "./common";

export function useProjectGitConnection(pid?: string | null) {
  const client = useQueryClient();
  const query = useQuery({
    queryKey: ["project-git", pid],
    queryFn: () => api.gitSettings(pid!),
    enabled: !!pid,
    refetchOnMount: "always",
    refetchOnWindowFocus: "always",
  });
  const test = useMutation({
    mutationFn: ({
      projectId,
      version,
    }: {
      projectId: string;
      version: string;
    }) => api.testGit(projectId, version),
    onSuccess: (value, { projectId }) => {
      client.setQueryData(["project-git", projectId], value);
      client.invalidateQueries({ queryKey: ["push"] });
    },
    onError: (_, { projectId }) =>
      client.invalidateQueries({ queryKey: ["project-git", projectId] }),
  });
  const testing = test.variables?.projectId === pid && test.isPending;
  const testError = test.variables?.projectId === pid ? test.error : null;
  const check = () =>
    test.mutate({ projectId: pid!, version: query.data!.version });
  const value = query.data;
  const reason =
    query.isFetching || testing
      ? "Git 연결 상태를 확인하고 있습니다."
      : testError
        ? "Git 연결 테스트에 실패했습니다. 다시 확인하세요."
        : query.error
          ? "Git 연결 상태를 조회하지 못했습니다."
          : !value?.remote_url
            ? "프로젝트에서 Push 원격 저장소를 설정하세요."
            : !value.test || value.test.url !== value.remote_url
              ? "Push 전에 Git 연결 테스트를 실행하세요."
              : value.test.status !== "success"
                ? value.test.message
                : null;
  return {
    pid,
    query,
    testing,
    testError,
    check,
    reason,
    ready: !!pid && !reason,
  };
}

export function ProjectGitConnection({
  connection: c,
}: {
  connection: ReturnType<typeof useProjectGitConnection>;
}) {
  if (!c.pid) return null;
  return (
    <div className="help" aria-label="Git 연결 상태">
      <p role="status">
        {c.reason ??
          "Git 연결 확인됨. 최종 승인·완료 후 작업 브랜치를 수동 Push할 수 있습니다."}
      </p>
      <p>
        조회 성공은 계정 인증이나 Push 권한을 보장하지 않습니다. 토큰이 없어도
        SSH·credential helper를 사용할 수 있습니다.
      </p>
      <Link to={`/projects/${encodeURIComponent(c.pid)}?tab=git`}>
        Git 계정·인증 설정
      </Link>{" "}
      <Button
        type="button"
        variant="ghost"
        disabled={!c.query.data?.remote_url || c.query.isFetching || c.testing}
        onClick={c.check}
      >
        {c.testing ? "연결 테스트 중…" : "Git 연결 테스트"}
      </Button>
      {c.query.error && (
        <ErrorNotice error={c.query.error} retry={() => c.query.refetch()} />
      )}
      {c.testError && <ErrorNotice error={c.testError} />}
    </div>
  );
}
