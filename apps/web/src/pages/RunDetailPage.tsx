import { useState, type FormEvent } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import * as Tabs from "@radix-ui/react-tabs";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  ArrowLeft,
  Check,
  CheckCircle2,
  Circle,
  CircleAlert,
  FileText,
  GitBranch,
  Pause,
  Play,
  Radio,
  RefreshCw,
  ShieldCheck,
  Square,
  Terminal,
} from "lucide-react";
import { api } from "../api/resources";
import { ApiError } from "../api/client";
import type { RunDetail } from "../api/types";
import {
  active,
  ErrorNotice,
  Heading,
  Loading,
  roles,
  Status,
} from "../components/common";
import { Button } from "../components/ui/button";
import { Modal } from "../components/ui/dialog";
import { LogViewer } from "../components/LogViewer";
import { useRunEvents } from "../hooks/events";
type Action = "cancel" | "pause" | "resume" | "approve" | "reconcile";
const actionLabels: Record<Action, string> = {
  cancel: "작업 취소",
  pause: "일시중지",
  resume: "작업 재개",
  approve: "최종 승인",
  reconcile: "상태 확인·조정",
};
const tabs = ["logs", "tests", "review", "diff", "report", "spec"] as const;
const labels: Record<(typeof tabs)[number], string> = {
  logs: "실시간 로그",
  tests: "테스트",
  review: "코드 리뷰",
  diff: "변경 내용",
  report: "최종 보고서",
  spec: "요구사항",
};
export function RunDetailPage() {
  const { id = "" } = useParams();
  return <RunDetailBody key={id} rid={id} />;
}
function RunDetailBody({ rid }: { rid: string }) {
  const client = useQueryClient(),
    [params, setParams] = useSearchParams();
  const selected = params.get("tab") ?? "logs";
  const tab = tabs.includes(selected as (typeof tabs)[number])
    ? selected
    : "logs";
  const [action, setAction] = useState<{ kind: Action; run: RunDetail } | null>(
      null,
    ),
    [reason, setReason] = useState(""),
    [sha, setSha] = useState(""),
    [budget, setBudget] = useState("");
  const stream = useRunEvents(rid);
  const run = useQuery({
    queryKey: ["run", rid],
    queryFn: () => api.run(rid),
    refetchInterval: (query) =>
      !query.state.data ||
      active(query.state.data.state) ||
      stream.connection !== "live"
        ? 3000
        : false,
  });
  const steps = useQuery({
    queryKey: ["steps", rid],
    queryFn: () => api.steps(rid),
    enabled: !!run.data,
  });
  const report = useQuery({
    queryKey: ["report", rid, run.data?.version],
    queryFn: () => api.report(rid),
    enabled: tab === "report" && !!run.data,
    retry: false,
  });
  const spec = useQuery({
    queryKey: ["spec", rid],
    queryFn: () => api.spec(rid),
    enabled: tab === "spec" && !!run.data,
  });
  const control = useMutation({
    mutationFn: ({ kind, run: target }: { kind: Action; run: RunDetail }) =>
      api.control(
        rid,
        target.version,
        kind,
        kind === "approve"
          ? { expected_sha: target.current_sha }
          : kind === "resume"
            ? {
                answers_text: reason || null,
                new_max_fix_attempts: budget ? Number(budget) : null,
              }
            : { reason },
      ),
    onSuccess: () => {
      setAction(null);
      client.invalidateQueries({ queryKey: ["run", rid] });
      client.invalidateQueries({ queryKey: ["runs"] });
      client.invalidateQueries({ queryKey: ["steps", rid] });
      client.invalidateQueries({ queryKey: ["dashboard"] });
    },
    onError: () => client.invalidateQueries({ queryKey: ["run", rid] }),
  });
  function open(kind: Action) {
    if (!run.data) return;
    control.reset();
    setReason("");
    setBudget("");
    setSha("");
    setAction({ kind, run: run.data });
  }
  function confirm(event: FormEvent) {
    event.preventDefault();
    if (
      action &&
      !control.isPending &&
      action.run.version === run.data?.version &&
      action.run.current_sha === run.data?.current_sha &&
      (action.kind !== "approve" || sha === action.run.current_sha)
    )
      control.mutate(action);
  }
  if (run.isPending) return <Loading />;
  if (run.error)
    return <ErrorNotice error={run.error} retry={() => run.refetch()} />;
  const r = run.data;
  const registered = !!r.project_id;
  const stale =
    !!action &&
    (action.run.version !== r.version ||
      action.run.current_sha !== r.current_sha);
  const reportMissing =
    report.error instanceof ApiError && report.error.status === 404;
  return (
    <>
      <Link className="back-link" to="/">
        <ArrowLeft size={14} />
        작업 목록으로
      </Link>
      <Heading eyebrow="WORKSPACE / RUN DETAIL" title={rid}>
        <Status state={r.state} />
      </Heading>
      <div className="run-overview panel">
        <div>
          <span className="subtle">Feature 브랜치</span>
          <strong className="mono">
            <GitBranch size={14} />
            {r.branch ?? "준비 중"}
          </strong>
        </div>
        <div>
          <span className="subtle">기준 브랜치</span>
          <strong>{r.base_branch ?? "기록 없음"}</strong>
        </div>
        <div>
          <span className="subtle">수정 회차</span>
          <strong>
            {r.iteration} / {r.max_fix_attempts ?? "미상"}
          </strong>
        </div>
        <div>
          <span className="subtle">생성 시각</span>
          <strong>
            {r.created_at
              ? new Date(r.created_at).toLocaleString("ko-KR")
              : "기록 없음"}
          </strong>
        </div>
      </div>
      <div className="run-actions">
        <span
          className={`connection connection-${stream.connection}`}
          role="status"
        >
          <Radio size={14} />
          {stream.connection === "live"
            ? "실시간 연결됨"
            : stream.connection === "connecting"
              ? "연결 중…"
              : "재연결 중 · 주기적 조회"}
        </span>
        <div className="inline-actions">
          {registered && (
            <>
              {active(r.state) && (
                <Button
                  variant="secondary"
                  disabled={!!r.control}
                  onClick={() => open("pause")}
                >
                  <Pause size={14} />
                  일시중지
                </Button>
              )}
              {["failed", "paused", "needs_input"].includes(r.state) && (
                <Button
                  variant="secondary"
                  disabled={r.control === "cancel"}
                  onClick={() => open("resume")}
                >
                  <Play size={14} />
                  재개
                </Button>
              )}
              {!["completed", "cancelled"].includes(r.state) && (
                <Button
                  variant="ghost"
                  disabled={!!r.control}
                  onClick={() => open("cancel")}
                >
                  <Square size={14} />
                  취소
                </Button>
              )}
              {r.state === "waiting_approval" && (
                <Button
                  disabled={!r.current_sha || !!r.control}
                  onClick={() => open("approve")}
                >
                  <ShieldCheck size={15} />
                  최종 승인
                </Button>
              )}
              <Button variant="ghost" onClick={() => open("reconcile")}>
                <RefreshCw size={14} />
                상태 조정
              </Button>
            </>
          )}
        </div>
      </div>
      {!registered && (
        <p className="notice">
          CLI 작업 · 조회 전용입니다. 실행 제어는 CLI에서 진행해 주세요.
        </p>
      )}
      {r.control &&
        !(r.control === "cancel" && r.state === "cancelled") &&
        !(r.control === "pause" && r.state === "paused") && (
          <p role="status" className="notice">
            {r.control === "cancel" ? "취소" : "일시중지"} 요청이
            접수되었습니다. 확정 상태는 Worker 정리 후 표시됩니다.
          </p>
        )}
      {r.error && <ErrorNotice error={new Error(r.error)} />}{" "}
      {!!r.questions.length && (
        <section className="notice">
          <CircleAlert size={18} />
          <div>
            <strong>답변이 필요한 질문</strong>
            <ul>
              {r.questions.map((q, i) => (
                <li key={i}>{q}</li>
              ))}
            </ul>
          </div>
        </section>
      )}
      <section className="panel timeline-panel">
        <div className="panel-heading">
          <h2>워크플로 타임라인</h2>
          <span className="subtle text-small">
            회차·시도별 이력 보존 · 단계 완료와 검증 통과를 구분합니다.
          </span>
        </div>
        {steps.isPending ? (
          <Loading />
        ) : steps.error ? (
          <ErrorNotice error={steps.error} retry={() => steps.refetch()} />
        ) : steps.data.items.length ? (
          <ol className="timeline">
            {steps.data.items.map((step) => (
              <li key={step.id} className={`timeline-step step-${step.status}`}>
                <span className="step-icon">
                  {step.status === "completed" ? (
                    <Check size={14} />
                  ) : step.status === "running" ? (
                    <Radio size={14} />
                  ) : (
                    <CircleAlert size={14} />
                  )}
                </span>
                <div>
                  <strong>{roles[step.role] ?? step.role}</strong>
                  <span className="subtle text-small">
                    회차 {step.iteration} · 시도 {step.attempt} ·{" "}
                    {step.status === "completed"
                      ? "완료"
                      : step.status === "running"
                        ? "진행 중"
                        : "중단됨"}
                  </span>
                  <code>
                    {(step.output_sha ?? step.input_sha).slice(0, 10)}
                  </code>
                </div>
              </li>
            ))}
            <li
              className={`timeline-step ${r.state === "completed" ? "step-completed" : ""}`}
            >
              <span className="step-icon">
                {r.state === "completed" ? (
                  <CheckCircle2 size={14} />
                ) : (
                  <Circle size={14} />
                )}
              </span>
              <div>
                <strong>Human approval</strong>
                <span className="subtle text-small">
                  {r.state === "completed" ? "승인 완료" : "사용자 확인"}
                </span>
              </div>
            </li>
          </ol>
        ) : (
          <p className="panel-body subtle">
            실행 시작 후 단계별 이력이 표시됩니다.
          </p>
        )}
      </section>
      <section className="panel results-panel">
        <Tabs.Root
          value={tab}
          onValueChange={(value) => setParams({ tab: value })}
        >
          <Tabs.List className="tabs-list" aria-label="실행 결과">
            {tabs.map((key) => (
              <Tabs.Trigger key={key} value={key} className="tab">
                {key === "logs" && <Terminal size={14} />} {labels[key]}
              </Tabs.Trigger>
            ))}
          </Tabs.List>
          <Tabs.Content value="logs" className="tab-content">
            <LogViewer
              logs={stream.logs}
              removed={stream.removed}
              unavailable={stream.unavailable}
            />
          </Tabs.Content>
          {(["tests", "review", "diff"] as const).map((key) => (
            <Tabs.Content key={key} value={key} className="tab-content">
              <div className="unavailable">
                <FileText size={24} />
                <h3>{labels[key]} 상세 조회를 준비하고 있습니다</h3>
                <p>
                  현재 백엔드는 이 상세 결과 API를 제공하지 않습니다. 완료
                  상태나 결과 0건으로 표시하지 않습니다.
                </p>
                <p className="subtle">
                  단계 실행 이력·로그·최종 보고서를 확인해 주세요. 상세 결과와
                  수정 요청은 후속 통합 단계에서 연결합니다.
                </p>
                {key === "review" && (
                  <p className="help">
                    다음 리뷰에서 이슈가 보이지 않는 것만으로 해결됨으로
                    판정하지 않습니다.
                  </p>
                )}
              </div>
            </Tabs.Content>
          ))}
          <Tabs.Content value="report" className="tab-content">
            {report.isPending ? (
              <Loading />
            ) : reportMissing ? (
              <p className="unavailable">아직 생성된 보고서가 없습니다.</p>
            ) : report.error ? (
              <ErrorNotice
                error={report.error}
                retry={() => report.refetch()}
              />
            ) : (
              <article className="markdown">
                <ReactMarkdown
                  remarkPlugins={[remarkGfm]}
                  skipHtml
                  components={{
                    a: ({ children }) => <span>{children}</span>,
                    img: () => null,
                  }}
                >
                  {report.data.markdown}
                </ReactMarkdown>
              </article>
            )}
          </Tabs.Content>
          <Tabs.Content value="spec" className="tab-content">
            {spec.isPending ? (
              <Loading />
            ) : spec.error ? (
              <ErrorNotice error={spec.error} retry={() => spec.refetch()} />
            ) : (
              <div className="spec-view">
                <h3>저장된 PRD</h3>
                <pre>{spec.data.text}</pre>
                {spec.data.additional_instructions && (
                  <>
                    <h3>추가 지시사항</h3>
                    <pre>{spec.data.additional_instructions}</pre>
                  </>
                )}
              </div>
            )}
          </Tabs.Content>
        </Tabs.Root>
      </section>
      <Modal
        open={!!action}
        onOpenChange={(value) => {
          if (!value && !control.isPending) setAction(null);
        }}
        title={action ? actionLabels[action.kind] : ""}
        description={
          action?.kind === "approve"
            ? "현재 후보 커밋을 테스트·리뷰 증거와 다시 대조하여 승인합니다."
            : action?.kind === "reconcile"
              ? "Worker 소유권·종료 상태·승인 유효성을 서버에서 명시적으로 확인합니다."
              : "현재 버전의 작업에 요청을 보냅니다. 확정 상태는 서버 응답으로 확인합니다."
        }
      >
        <form onSubmit={confirm} className="form-grid">
          {action?.kind === "approve" ? (
            <>
              <p className="mono path approval-sha">{action.run.current_sha}</p>
              <label>
                위 커밋 SHA 전체를 입력해 확인하세요
                <input
                  value={sha}
                  onChange={(e) => setSha(e.target.value)}
                  autoComplete="off"
                  required
                />
              </label>
            </>
          ) : action?.kind === "resume" ? (
            <>
              <label>
                질문 답변·추가 지시 (선택)
                <textarea
                  value={reason}
                  maxLength={65536}
                  onChange={(e) => setReason(e.target.value)}
                />
              </label>
              <label>
                새 최대 수정 횟수 (한도 증가 시)
                <input
                  type="number"
                  min={(r.max_fix_attempts ?? 0) + 1}
                  disabled={r.max_fix_attempts === null}
                  max={20}
                  value={budget}
                  onChange={(e) => setBudget(e.target.value)}
                />
              </label>
            </>
          ) : (
            action?.kind !== "reconcile" && (
              <label>
                사유 (선택)
                <textarea
                  value={reason}
                  maxLength={4000}
                  onChange={(e) => setReason(e.target.value)}
                />
              </label>
            )
          )}
          {stale && (
            <p role="alert" className="notice">
              작업 상태가 변경되었습니다. 대화상자를 닫고 최신 결과를 다시
              확인해 주세요.
            </p>
          )}
          {control.error && <ErrorNotice error={control.error} />}
          <Button
            type="submit"
            variant={action?.kind === "cancel" ? "danger" : "default"}
            disabled={
              control.isPending ||
              stale ||
              (action?.kind === "approve" && sha !== action.run.current_sha)
            }
          >
            {control.isPending
              ? "처리 중…"
              : action
                ? `${actionLabels[action.kind]} 확인`
                : "확인"}
          </Button>
        </form>
      </Modal>
    </>
  );
}
