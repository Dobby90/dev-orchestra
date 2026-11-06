import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/resources";
import type { Schema } from "../api/types";
import { ErrorNotice, Loading } from "./common";
import { Button } from "./ui/button";

export function ProjectGitSettings({ pid }: { pid: string }) {
  const query = useQuery({
    queryKey: ["project-git", pid],
    queryFn: () => api.gitSettings(pid),
  });
  if (query.isPending) return <Loading />;
  if (query.error)
    return <ErrorNotice error={query.error} retry={() => query.refetch()} />;
  return <GitForm key={query.data.version} pid={pid} value={query.data} />;
}

function GitForm({
  pid,
  value,
}: {
  pid: string;
  value: Schema["GitSettingsView"];
}) {
  const client = useQueryClient();
  const [remote, setRemote] = useState(value.remote);
  const [account, setAccount] = useState(value.account);
  const [token, setToken] = useState("");
  const [removeToken, setRemoveToken] = useState(false);
  const changed =
    remote !== value.remote ||
    account !== value.account ||
    !!token ||
    removeToken;
  const update = (data: Schema["GitSettingsView"]) => {
    setToken("");
    client.setQueryData(["project-git", pid], data);
    client.invalidateQueries({ queryKey: ["push"] });
  };
  const save = useMutation({
    mutationFn: () => {
      const body = {
        expected_version: value.version,
        remote,
        account,
        token: token || null,
        delete_token: removeToken,
      };
      setToken("");
      return api.saveGitSettings(pid, body);
    },
    onSuccess: update,
    onError: () => client.invalidateQueries({ queryKey: ["project-git", pid] }),
  });
  const test = useMutation({
    mutationFn: () => api.testGit(pid, value.version),
    onSuccess: update,
  });
  const pending = save.isPending || test.isPending;
  return (
    <section className="panel panel-body" aria-label="프로젝트 Git 인증">
      <h2>Git 계정 및 인증</h2>
      <p className="help">
        Push에 사용할 인증 계정입니다. 커밋 작성자 user.name·user.email과 전역
        Git 설정은 변경하지 않습니다. 토큰이 없으면 기존 SSH·credential helper를
        사용합니다.
      </p>
      <form
        className="form-grid"
        onSubmit={(e) => {
          e.preventDefault();
          save.mutate();
        }}
      >
        <label>
          Push 원격 저장소
          <select
            aria-label="Push 원격 저장소"
            value={remote}
            disabled={pending}
            onChange={(e) => setRemote(e.target.value)}
          >
            <option value="">선택하세요</option>
            {value.remotes.map((r) => (
              <option key={r.name} value={r.name}>
                {r.name}
              </option>
            ))}
          </select>
        </label>
        <p className="mono path">
          원격 주소:{" "}
          {value.remotes.find((r) => r.name === remote)?.url ?? "선택 필요"}
        </p>
        <label>
          Git 인증 계정 ID
          <input
            aria-label="Git 인증 계정 ID"
            autoComplete="off"
            value={account}
            disabled={pending}
            onChange={(e) => setAccount(e.target.value)}
          />
        </label>
        <p>
          토큰: {value.token_registered ? "등록됨 (원문 조회 불가)" : "미등록"}
        </p>
        <label>
          인증 토큰 등록·교체
          <input
            aria-label="인증 토큰 등록·교체"
            type="password"
            autoComplete="new-password"
            value={token}
            disabled={pending || removeToken}
            onChange={(e) => setToken(e.target.value)}
          />
        </label>
        {value.token_registered && (
          <label className="check-row">
            <input
              type="checkbox"
              checked={removeToken}
              disabled={pending}
              onChange={(e) => {
                setRemoveToken(e.target.checked);
                setToken("");
              }}
            />
            저장된 토큰 삭제
          </label>
        )}
        <Button type="submit" disabled={pending || !remote || !changed}>
          {save.isPending ? "저장 중…" : "Git 설정 저장"}
        </Button>
        <Button
          type="button"
          variant="secondary"
          disabled={pending || changed || !value.remote_url}
          onClick={() => test.mutate()}
        >
          {test.isPending ? "연결 테스트 중…" : "연결 테스트"}
        </Button>
      </form>
      {(save.error || test.error) && (
        <ErrorNotice error={save.error || test.error} />
      )}
      <div role="status">
        {changed ? (
          <p>변경 사항을 저장한 뒤 연결을 다시 테스트하세요.</p>
        ) : value.test ? (
          <>
            <p>마지막 연결 테스트: {value.test.checked_at}</p>
            <p>
              {value.test.status === "success"
                ? "저장소 조회 성공"
                : value.test.message}
            </p>
          </>
        ) : (
          <p>유효한 연결 테스트 결과가 없습니다.</p>
        )}
        <p className="help">
          Push 권한: 확인되지 않음. 연결 테스트는 원격 브랜치를 변경하지 않으며,
          조회 성공만으로 Push 권한을 보장하지 않습니다.
        </p>
      </div>
    </section>
  );
}
