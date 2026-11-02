import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Download, FileCode2, ShieldCheck } from "lucide-react";
import { api } from "../api/resources";
import type { Schema } from "../api/types";
import { Empty, ErrorNotice, Loading } from "./common";
import { Button } from "./ui/button";
import { Modal } from "./ui/dialog";
const statuses: Record<string, string> = {
  not_run: "아직 실행되지 않음",
  running: "실행 중",
  passed: "테스트 통과",
  failed: "테스트 실패",
  unavailable: "실행·증거 확인 불가",
  parse_error: "JUnit 파싱 실패",
  timeout: "시간 초과",
  unknown: "판정 정보 없음",
};
const artifact = (rid: string, aid: string) =>
  `/api/runs/${encodeURIComponent(rid)}/artifacts/${encodeURIComponent(aid)}`;
function Attempts({
  history,
  value,
  onChange,
}: {
  history: Schema["StepStamp"][];
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <select
      aria-label="결과 회차 선택"
      value={value}
      onChange={(e) => onChange(e.target.value)}
    >
      <option value="">현재 revision의 최신 결과</option>
      {history.map((s) => (
        <option key={s.id} value={s.id}>
          revision {s.revision ?? "미상"} · 회차 {s.iteration} · 시도{" "}
          {s.attempt} · {s.status}
        </option>
      ))}
    </select>
  );
}
export function TestsPanel({
  rid,
  version,
  revision,
}: {
  rid: string;
  version: number;
  revision: number;
}) {
  const [selected, setSelected] = useState("");
  const result = useQuery({
    queryKey: ["tests", rid, version, selected],
    queryFn: () => api.tests(rid, selected ? Number(selected) : undefined),
  });
  if (result.isPending) return <Loading />;
  if (result.error)
    return <ErrorNotice error={result.error} retry={() => result.refetch()} />;
  const data = result.data;
  return (
    <div className="result-detail">
      <div className="panel-heading">
        <h2>{statuses[data.status] ?? data.status}</h2>
        <Attempts
          history={data.history}
          value={selected}
          onChange={setSelected}
        />
      </div>
      {data.revision !== null && data.revision !== revision && (
        <p className="notice">
          이전 revision {data.revision}의 결과입니다. 현재 revision {revision}의
          검증으로 사용하지 않습니다.
        </p>
      )}
      {data.legacy_incomplete && data.step_id && (
        <p className="notice">
          과거 결과에 없는 시간·실패 상세는 미상으로 표시합니다.
        </p>
      )}
      <div className="result-counts">
        {[
          { name: "전체", n: data.total },
          { name: "성공", n: data.passed },
          { name: "실패", n: data.failed },
          { name: "건너뜀", n: data.skipped },
        ].map((v) => (
          <div key={v.name}>
            <span>{v.name}</span>
            <strong>{v.n ?? "미상"}</strong>
          </div>
        ))}
      </div>
      {data.sha && (
        <p className="mono help path">
          검증 SHA {data.sha} · 소요 시간{" "}
          {data.duration_seconds === null
            ? "미상"
            : `${data.duration_seconds.toFixed(2)}초`}
        </p>
      )}
      {data.commands.map((c, index) => (
        <section className="command-result" key={c.command_id ?? index}>
          <header>
            <h3>{c.command_id ?? "과거 명령"}</h3>
            <span className={c.passed === true ? "text-green" : "text-amber"}>
              {statuses[c.status] ?? c.status}
            </span>
          </header>
          <pre>{c.argv?.join(" ") ?? "명령 인자 기록 없음"}</pre>
          <p className="help">
            종료 코드 {c.exit_code ?? "미상"} ·{" "}
            {c.duration_seconds == null
              ? "시간 미상"
              : `${c.duration_seconds.toFixed(2)}초`}
          </p>
          {c.error && <p className="notice">{c.error}</p>}
          {c.junit_artifact_id && (
            <Button asChild variant="secondary">
              <a href={artifact(rid, c.junit_artifact_id)}>
                <Download size={14} />
                보존된 JUnit
              </a>
            </Button>
          )}
          {(c.failure_details ?? []).map((f, i) => (
            <details key={i} className="failure">
              <summary>
                {f.name} · {f.message || "실패 상세"}
              </summary>
              {f.file && (
                <p className="mono help">
                  {f.file}:{f.line ?? "미상"}
                </p>
              )}
              <pre>{f.details}</pre>
            </details>
          ))}
        </section>
      ))}
    </div>
  );
}
export function ReviewPanel({
  rid,
  version,
  revision,
  canClassify,
}: {
  rid: string;
  version: number;
  revision: number;
  canClassify: boolean;
}) {
  const [selected, setSelected] = useState(""),
    [issue, setIssue] = useState<
      (Schema["ReviewIssueView"] & { version: number }) | null
    >(null),
    [reason, setReason] = useState(""),
    [key, setKey] = useState("");
  const client = useQueryClient();
  const result = useQuery({
    queryKey: ["reviews", rid, version, selected],
    queryFn: () => api.reviews(rid, selected ? Number(selected) : undefined),
  });
  const disposition = useMutation({
    mutationFn: () =>
      api.disposition(
        rid,
        issue!.issue_id,
        issue!.version,
        {
          status: issue!.status === "dismissed" ? "unresolved" : "dismissed",
          reason,
        },
        key,
      ),
    onSuccess: () => {
      setIssue(null);
      client.invalidateQueries({ queryKey: ["run", rid] });
      client.invalidateQueries({ queryKey: ["reviews", rid] });
    },
  });
  if (result.isPending) return <Loading />;
  if (result.error)
    return <ErrorNotice error={result.error} retry={() => result.refetch()} />;
  const data = result.data;
  return (
    <div className="result-detail">
      <div className="panel-heading">
        <h2>
          <ShieldCheck size={17} />
          독립 코드 리뷰
        </h2>
        <Attempts
          history={data.history}
          value={selected}
          onChange={setSelected}
        />
      </div>
      {data.status !== "available" ? (
        <Empty
          title={
            data.status === "running"
              ? "리뷰 진행 중"
              : data.status === "not_run"
                ? "리뷰 결과 없음"
                : "리뷰 증거를 읽을 수 없습니다"
          }
        />
      ) : (
        <>
          {data.revision !== revision && (
            <p className="notice">
              선택한 revision {data.revision}의 과거 리뷰입니다.
            </p>
          )}
          {data.legacy_incomplete && (
            <p className="notice">
              과거 V1 리뷰입니다. 이슈가 사라진 것을 해결 근거로 사용하지
              않습니다.
            </p>
          )}
          <p>{data.summary}</p>
          <p className="mono help path">리뷰 SHA {data.sha}</p>
          {data.issues.length ? (
            data.issues.map((item) => (
              <article className="review-issue" key={item.issue_id}>
                <header>
                  <strong
                    className={
                      ["critical", "high"].includes(item.severity)
                        ? "text-red"
                        : "text-amber"
                    }
                  >
                    {item.severity.toUpperCase()}
                  </strong>
                  <span
                    className={
                      item.status === "resolved" ? "text-green" : "text-amber"
                    }
                  >
                    {item.status === "resolved"
                      ? "독립 재검증으로 해결"
                      : item.status === "dismissed"
                        ? "사용자 제외"
                        : "미해결"}
                  </span>
                </header>
                <h3>{item.description}</h3>
                <p className="mono help path">
                  {item.file}:{item.line} · 발견 SHA{" "}
                  {item.source_sha.slice(0, 12)}
                </p>
                <p>{item.evidence}</p>
                <p className="subtle">제안: {item.suggestion}</p>
                {item.resolution_evidence && (
                  <div className="notice">
                    <ShieldCheck size={16} />
                    <div>
                      <p>{item.resolution_evidence}</p>
                      <code>{item.assessed_sha}</code>
                    </div>
                  </div>
                )}
                {canClassify &&
                  !selected &&
                  !data.blocking_severities.includes(item.severity) &&
                  !data.legacy_incomplete && (
                    <Button
                      variant="ghost"
                      onClick={() => {
                        disposition.reset();
                        setIssue({ ...item, version });
                        setReason("");
                        setKey(crypto.randomUUID());
                      }}
                    >
                      {item.status === "dismissed"
                        ? "분류 되돌리기"
                        : "비차단 이슈 제외 사유 기록"}
                    </Button>
                  )}
              </article>
            ))
          ) : (
            <Empty title="이 리뷰에서 차단 이슈가 없습니다">
              최종 승인은 동일 SHA의 테스트·리뷰·현재 작업 트리를 서버에서 다시
              확인합니다.
            </Empty>
          )}
        </>
      )}
      <Modal
        open={!!issue}
        onOpenChange={(open) => {
          if (!open && !disposition.isPending) setIssue(null);
        }}
        title="이슈 분류 사유"
        description="비차단 이슈의 사용자 판단을 기록합니다. 해결 근거로 처리하지 않습니다."
      >
        <form
          className="form-grid"
          onSubmit={(e) => {
            e.preventDefault();
            if (!disposition.isPending) disposition.mutate();
          }}
        >
          <label>
            사유
            <textarea
              required
              maxLength={4000}
              value={reason}
              disabled={disposition.isPending || !!disposition.error}
              onChange={(e) => setReason(e.target.value)}
            />
          </label>
          {disposition.error && <ErrorNotice error={disposition.error} />}
          <Button
            type="submit"
            disabled={disposition.isPending || !reason.trim()}
          >
            사유 기록
          </Button>
        </form>
      </Modal>
    </div>
  );
}
export function ChangesPanel({
  rid,
  version,
  revision,
}: {
  rid: string;
  version: number;
  revision: number;
}) {
  const [snapshot, setSnapshot] = useState(""),
    [file, setFile] = useState("");
  const [chosenRev, chosenSha] = snapshot
    ? snapshot.split(":")
    : [String(revision), undefined];
  const changes = useQuery({
    queryKey: ["changes", rid, version, snapshot],
    queryFn: () => api.changes(rid, Number(chosenRev), chosenSha),
  });
  const candidate = changes.data?.candidate_sha;
  const selected =
    changes.data?.files.find((f) => f.file_id === file) ??
    changes.data?.files[0];
  const diff = useQuery({
    queryKey: ["diff", rid, candidate, selected?.file_id, chosenRev],
    queryFn: () =>
      api.diff(rid, selected!.file_id, candidate!, Number(chosenRev)),
    enabled: !!selected && !!candidate,
  });
  if (changes.isPending) return <Loading />;
  if (changes.error)
    return (
      <ErrorNotice error={changes.error} retry={() => changes.refetch()} />
    );
  const data = changes.data;
  return (
    <div className="result-detail">
      <div className="panel-heading">
        <h2>
          <FileCode2 size={17} />
          보존된 후보 변경 내용
        </h2>
        <select
          aria-label="변경 내용 revision 선택"
          value={snapshot}
          onChange={(e) => {
            setSnapshot(e.target.value);
            setFile("");
          }}
        >
          <option value="">현재 revision {revision}</option>
          {data.history.map((s) => (
            <option
              key={`${s.revision}:${s.candidate_sha}`}
              value={`${s.revision}:${s.candidate_sha}`}
            >
              revision {s.revision} · {s.candidate_sha.slice(0, 10)}
            </option>
          ))}
        </select>
      </div>
      {!data.available ? (
        <Empty title="보존된 후보 변경분이 없습니다">
          과거 실행의 누락 증거를 자동 생성하거나 현재 파일로 대체하지 않습니다.
        </Empty>
      ) : (
        <>
          <p className="help mono path">
            기준 {data.base_sha} → 후보 {data.candidate_sha}
          </p>
          {data.files.length ? (
            <div className="diff-grid">
              <nav aria-label="변경 파일">
                {data.files.map((f) => (
                  <button
                    key={f.file_id}
                    aria-pressed={f.file_id === selected?.file_id}
                    onClick={() => setFile(f.file_id)}
                  >
                    <span className="mono">{f.path}</span>
                    <span className="subtle">
                      {f.status} ·{" "}
                      {f.binary ? "binary" : `+${f.additions} −${f.deletions}`}
                    </span>
                  </button>
                ))}
              </nav>
              <section>
                {diff.isPending ? (
                  <Loading />
                ) : diff.error ? (
                  <ErrorNotice
                    error={diff.error}
                    retry={() => diff.refetch()}
                  />
                ) : (
                  <>
                    {diff.data.binary && (
                      <p className="notice">
                        바이너리 파일의 내용은 텍스트 Diff로 표시하지 않습니다.
                      </p>
                    )}
                    {diff.data.truncated && (
                      <p className="notice">
                        1 MiB를 초과하여 잘린 Diff입니다.
                      </p>
                    )}
                    <Button asChild variant="ghost">
                      <a href={artifact(rid, diff.data.artifact_id)}>
                        <Download size={14} />
                        보존 Diff 저장
                      </a>
                    </Button>
                    <pre
                      className="diff-text"
                      tabIndex={0}
                      aria-label="Git Diff"
                    >
                      {diff.data.text}
                    </pre>
                  </>
                )}
              </section>
            </div>
          ) : (
            <Empty title="이 후보의 변경 파일이 없습니다">
              변경이 없는 수정도 새 Test·Review 증거를 확인해야 합니다.
            </Empty>
          )}
        </>
      )}
    </div>
  );
}
