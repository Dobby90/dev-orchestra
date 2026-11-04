import {
  CheckCircle2,
  Circle,
  CircleAlert,
  CirclePause,
  LoaderCircle,
  XCircle,
  ArrowUpRight,
} from "lucide-react";
import { Link } from "react-router-dom";
import type { ReactNode } from "react";
import type { Run, State } from "../api/types";
import { Button } from "./ui/button";
export const states: Record<State, { label: string; tone: string }> = {
  queued: { label: "실행 대기", tone: "muted" },
  preparing: { label: "준비 중", tone: "blue" },
  waiting_plan_approval: { label: "기획 승인 대기", tone: "amber" },
  planning: { label: "기획 중", tone: "blue" },
  developing: { label: "개발 중", tone: "blue" },
  testing: { label: "테스트 중", tone: "blue" },
  reviewing: { label: "리뷰 중", tone: "blue" },
  fixing: { label: "수정 중", tone: "blue" },
  reporting: { label: "보고서 작성", tone: "blue" },
  waiting_approval: { label: "승인 대기", tone: "amber" },
  completed: { label: "완료", tone: "green" },
  failed: { label: "실패", tone: "red" },
  needs_input: { label: "입력 필요", tone: "amber" },
  paused: { label: "일시중지", tone: "muted" },
  cancelled: { label: "취소됨", tone: "muted" },
};
export const roles: Record<string, string> = {
  planner: "Planning",
  developer: "Developer",
  tester: "Testing",
  reviewer: "Review",
  fixer: "Fixing",
};
export function active(state: State) {
  return [
    "queued",
    "preparing",
    "planning",
    "developing",
    "testing",
    "reviewing",
    "fixing",
    "reporting",
  ].includes(state);
}
export function Status({
  state,
  large = false,
}: {
  state: State;
  large?: boolean;
}) {
  const { label, tone } = states[state];
  const Icon =
    state === "completed"
      ? CheckCircle2
      : state === "failed"
        ? XCircle
        : state === "paused"
          ? CirclePause
          : tone === "amber"
            ? CircleAlert
            : active(state) && state !== "queued"
              ? LoaderCircle
              : Circle;
  return (
    <span className={`status status-${tone}${large ? " status-large" : ""}`}>
      <Icon size={large ? 18 : 13} className={tone === "blue" ? "spin" : ""} />
      {label}
    </span>
  );
}
export function Heading({
  eyebrow,
  title,
  description,
  children,
}: {
  eyebrow?: string;
  title: string;
  description?: string;
  children?: ReactNode;
}) {
  return (
    <header className="page-heading">
      <div>
        {eyebrow && <p className="eyebrow">{eyebrow}</p>}
        <h1>{title}</h1>
        {description && <p className="subtle">{description}</p>}
      </div>
      {children}
    </header>
  );
}
export function ErrorNotice({
  error,
  retry,
}: {
  error: unknown;
  retry?: () => void;
}) {
  return (
    <div role="alert" className="notice notice-error">
      <CircleAlert size={18} />
      <div>
        <strong>확인이 필요합니다</strong>
        <p>
          {error instanceof Error
            ? error.message
            : "요청을 처리하지 못했습니다."}
        </p>
        {retry && (
          <Button variant="secondary" onClick={retry}>
            다시 시도
          </Button>
        )}
      </div>
    </div>
  );
}
export function Empty({
  title,
  children,
}: {
  title: string;
  children?: ReactNode;
}) {
  return (
    <div className="empty">
      <Circle size={28} />
      <h3>{title}</h3>
      {children && <p className="subtle">{children}</p>}
    </div>
  );
}
export function Loading() {
  return (
    <div role="status" className="loading">
      <LoaderCircle className="spin" size={18} />
      불러오는 중…
    </div>
  );
}
export function RunsTable({ runs }: { runs: Run[] }) {
  return runs.length ? (
    <div className="table-scroll">
      <table>
        <thead>
          <tr>
            <th>작업 / 브랜치</th>
            <th>상태</th>
            <th>현재 단계</th>
            <th>수정 회차</th>
            <th>
              <span className="sr-only">상세</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {runs.map((run) => (
            <tr key={run.id}>
              <td>
                <Link to={`/runs/${run.id}`} className="run-link">
                  {run.id}
                </Link>
                <div className="mono subtle text-small">
                  {run.branch ?? "브랜치 준비 중"}
                </div>
              </td>
              <td>
                <Status state={run.state} />
                {run.control && (
                  <span className="text-small subtle">
                    {run.control === "cancel" ? "취소 요청됨" : "중지 요청됨"}
                  </span>
                )}
              </td>
              <td>
                {run.current_role
                  ? (roles[run.current_role] ?? run.current_role)
                  : "—"}
              </td>
              <td className="mono">{run.iteration}</td>
              <td>
                <Link aria-label={`${run.id} 상세 보기`} to={`/runs/${run.id}`}>
                  <ArrowUpRight size={18} />
                </Link>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  ) : (
    <Empty title="표시할 작업이 없습니다">
      새 작업을 만들면 실행 상태와 결과를 이곳에서 확인할 수 있습니다.
    </Empty>
  );
}
