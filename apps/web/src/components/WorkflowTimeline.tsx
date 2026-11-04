import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Check, Circle, CircleAlert, Radio } from "lucide-react";
import { api } from "../api/resources";
import type { RunDetail, Schema } from "../api/types";
import { roles, states } from "./common";

type Step = Schema["StepView"];
const statusLabels: Record<string, string> = {
  completed: "실행 완료",
  running: "진행 중",
  failed: "실행·증거 오류",
  interrupted: "중단됨",
};
const testLabels: Record<string, string> = {
  passed: "테스트 통과",
  failed: "테스트 실패",
  running: "검증 진행 중",
  not_run: "검증 미실행",
  unavailable: "검증 확인 불가",
  parse_error: "JUnit 파싱 실패",
  timeout: "검증 시간 초과",
  unknown: "검증 결과 미상",
};
function elapsed(step: Step, now: number) {
  if (!step.started_at || (!step.finished_at && step.status !== "running"))
    return "시간 미상";
  const start = Date.parse(step.started_at);
  const end = step.finished_at ? Date.parse(step.finished_at) : now;
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start)
    return "시간 미상";
  const seconds = Math.floor((end - start) / 1000);
  return `${Math.floor(seconds / 60)}분 ${seconds % 60}초${step.finished_at ? " 소요" : " 경과"}`;
}
function StepRow({
  step,
  run,
  now,
  onSelect,
}: {
  step: Step;
  run: RunDetail;
  now: number;
  onSelect: (tab: string, step: number) => void;
}) {
  const tests = useQuery({
    queryKey: ["tests", run.id, run.version, String(step.id)],
    queryFn: () => api.tests(run.id, step.id),
    enabled: step.role === "tester" && step.status !== "running",
  });
  const result = tests.data?.status;
  return (
    <li
      className={`timeline-step step-${step.status}`}
      aria-current={step.status === "running" ? "step" : undefined}
    >
      <span className="step-icon" aria-hidden="true">
        {step.status === "completed" ? (
          <Check size={16} />
        ) : step.status === "running" ? (
          <Radio size={16} />
        ) : (
          <CircleAlert size={16} />
        )}
      </span>
      <div className="step-content">
        <strong>
          {roles[step.role] ?? step.role}{" "}
          <span className="step-status">
            {statusLabels[step.status] ?? "상태 미상"}
          </span>
        </strong>
        {step.role === "tester" && (
          <span
            className={`step-result ${result === "failed" ? "text-red" : result === "passed" ? "text-green" : "text-amber"}`}
          >
            {step.status === "running"
              ? "검증 진행 중"
              : tests.isPending
                ? "검증 결과 조회 중…"
                : tests.error
                  ? "검증 확인 불가"
                  : (testLabels[result ?? "unknown"] ?? "검증 결과 미상")}
          </span>
        )}
        <span className="subtle text-small">
          시도 {step.attempt} · {elapsed(step, now)}
        </span>
        <div className="inline-actions">
          <button
            type="button"
            className="step-link"
            onClick={() => onSelect("logs", step.id)}
          >
            단계 로그
          </button>
          {(step.role === "tester" || step.role === "reviewer") && (
            <button
              type="button"
              className="step-link"
              onClick={() =>
                onSelect(step.role === "tester" ? "tests" : "review", step.id)
              }
            >
              해당 시도 결과
            </button>
          )}
        </div>
        <details className="step-evidence">
          <summary>시간·SHA 상세</summary>
          <p>
            시작:{" "}
            {step.started_at
              ? new Date(step.started_at).toLocaleString("ko-KR")
              : "미상"}
            <br />
            종료:{" "}
            {step.finished_at
              ? new Date(step.finished_at).toLocaleString("ko-KR")
              : "미상"}
          </p>
          <code>입력 SHA: {step.input_sha}</code>
          <code>출력 SHA: {step.output_sha ?? "미상"}</code>
        </details>
      </div>
    </li>
  );
}
export function WorkflowTimeline({
  run,
  items,
  onSelect,
}: {
  run: RunDetail;
  items: Step[];
  onSelect: (tab: string, step: number) => void;
}) {
  const [now, setNow] = useState(Date.now);
  const [reveal, setReveal] = useState(0);
  const currentStep = [...items]
    .reverse()
    .find(
      (step) =>
        step.status === "running" &&
        (step.revision ?? 0) === (run.revision ?? 0),
    );
  const running = items.some((s) => s.status === "running");
  useEffect(() => {
    if (!running) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [running]);
  const groups = new Map<string, Step[]>();
  for (const step of items) {
    const key = `${step.revision ?? 0}:${step.iteration}`;
    groups.set(key, [...(groups.get(key) ?? []), step]);
  }
  const approval =
    run.state === "completed"
      ? "승인 완료"
      : run.state === "waiting_approval"
        ? "승인 대기"
        : ["cancelled", "failed"].includes(run.state)
          ? "작업 종료로 미진행"
          : "승인 단계 전";
  return (
    <div className="workflow-timeline">
      <p className="workflow-current" role="status">
        {states[run.state].label} · 현재 revision {run.revision ?? 0} ·{" "}
        {run.iteration === 0 ? "최초 실행" : `수정 ${run.iteration}회차`}
      </p>
      <p className="help">
        기획 → 기획 승인 → 개발 → 테스트 → 리뷰 → 보고서 → 최종 승인 · 수정 시
        수정 → 테스트 → 리뷰 반복
      </p>
      {currentStep && (
        <button
          type="button"
          className="step-link"
          onClick={() => setReveal((value) => value + 1)}
        >
          현재 단계로 이동
        </button>
      )}
      <div
        className="timeline-history"
        role="region"
        aria-label="워크플로 실행 이력"
        tabIndex={0}
      >
        {!items.length && (
          <p className="subtle">실행 시작 후 단계별 이력이 표시됩니다.</p>
        )}
        {[...groups].map(([key, steps]) => (
          <Group
            key={`${key}:${run.revision}:${run.iteration}`}
            steps={steps}
            run={run}
            now={now}
            onSelect={onSelect}
            reveal={
              steps.some((step) => step.id === currentStep?.id) ? reveal : 0
            }
          />
        ))}
      </div>
      <div
        className={`approval-step ${run.state === "waiting_approval" ? "approval-waiting" : ""}`}
      >
        <Circle size={16} aria-hidden="true" />
        <strong>최종 승인</strong>
        <span>{approval}</span>
      </div>
    </div>
  );
}
function Group({
  reveal,
  steps,
  run,
  now,
  onSelect,
}: {
  steps: Step[];
  run: RunDetail;
  now: number;
  onSelect: (tab: string, step: number) => void;
  reveal: number;
}) {
  const first = steps[0];
  const [expanded, setExpanded] = useState(
    (first.revision ?? 0) === (run.revision ?? 0) &&
      first.iteration === run.iteration,
  );
  const groupRef = useRef<HTMLDetailsElement>(null);
  useEffect(() => {
    if (reveal) setExpanded(true);
  }, [reveal]);
  useLayoutEffect(() => {
    if (!reveal || !expanded) return;
    const group = groupRef.current;
    const history = group?.closest<HTMLElement>(".timeline-history");
    const target = group?.querySelector<HTMLElement>('[aria-current="step"]');
    if (history && target) {
      history.scrollTo({
        top:
          history.scrollTop +
          target.getBoundingClientRect().top -
          history.getBoundingClientRect().top,
        behavior: "instant",
      });
    }
  }, [reveal, expanded]);
  return (
    <details
      ref={groupRef}
      className="timeline-group"
      open={expanded}
      onToggle={(event) => setExpanded(event.currentTarget.open)}
    >
      <summary>
        revision {first.revision ?? 0} ·{" "}
        {first.iteration === 0 ? "최초 실행" : `수정 ${first.iteration}회차`} ·{" "}
        {steps.length}개 시도
      </summary>
      {expanded && (
        <ol
          className="timeline"
          aria-label={`revision ${first.revision ?? 0} 회차 ${first.iteration} 실행 이력`}
        >
          {steps.map((step) => (
            <StepRow
              key={step.id}
              step={step}
              run={run}
              now={now}
              onSelect={onSelect}
            />
          ))}
        </ol>
      )}
    </details>
  );
}
