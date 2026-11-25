import { Link, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  ArrowUpRight,
  FolderGit2,
  GitBranch,
  Play,
  Plus,
  ShieldCheck,
  Workflow,
} from "lucide-react";
import { api } from "../api/resources";
import type { State } from "../api/types";
import {
  active,
  Heading,
  ErrorNotice,
  Loading,
  RunsTable,
  states,
} from "../components/common";
import { Button } from "../components/ui/button";

const groups: Partial<Record<string, State[]>> = {
  running: Object.keys(states).filter(
    (key) => active(key as State) && key !== "queued",
  ) as State[],
  approvals: ["waiting_approval", "waiting_plan_approval"],
  attention: ["failed", "needs_input"],
};

export function DashboardPage() {
  const [params, setParams] = useSearchParams();
  const selected = params.get("state") ?? "",
    cursor = params.get("cursor") ?? "";
  const query = new URLSearchParams({ limit: "30", cursor });
  if (Object.hasOwn(groups, selected)) {
    groups[selected]?.forEach((state) => query.append("states", state));
  } else if (Object.hasOwn(states, selected)) {
    query.set("state", selected);
  }
  const dashboard = useQuery({
    queryKey: ["dashboard"],
    queryFn: api.dashboard,
    refetchInterval: 5000,
  });
  const runs = useQuery({
    queryKey: ["runs", selected, cursor],
    queryFn: () => api.runs(query),
    refetchInterval: 5000,
  });
  const counts = dashboard.data?.counts_by_state;
  const nextCursor = runs.data?.next_cursor;
  function showPage(cursor?: string) {
    setParams((current) => {
      const next = new URLSearchParams(current);
      if (cursor) next.set("cursor", cursor);
      else next.delete("cursor");
      return next;
    });
  }
  const running = counts
    ? Object.entries(counts).reduce(
        (sum, [key, value]) =>
          sum + (active(key as State) && key !== "queued" ? (value ?? 0) : 0),
        0,
      )
    : null;
  return (
    <>
      <Heading
        eyebrow="WORKSPACE / OVERVIEW"
        title="작업 현황"
        description="진행 상황과 확인할 일을 모아봅니다."
      >
        <Button asChild>
          <Link to="/runs/new">
            <Plus size={16} />새 작업
          </Link>
        </Button>
      </Heading>
      {dashboard.error && (
        <ErrorNotice
          error={dashboard.error}
          retry={() => dashboard.refetch()}
        />
      )}
      <div className="stats-grid">
        {[
          {
            label: "등록 프로젝트",
            to: "/projects",
            value: dashboard.data?.project_count,
            icon: FolderGit2,
            note: "로컬 저장소",
            tone: "neutral",
          },
          {
            label: "실행 중",
            to: "/?state=running",
            value: running,
            icon: Play,
            note: "에이전트 작업 진행",
            tone: "blue",
          },
          {
            label: "승인 대기",
            to: "/?state=approvals",
            value: counts
              ? (counts.waiting_approval ?? 0) +
                (counts.waiting_plan_approval ?? 0)
              : null,
            icon: ShieldCheck,
            note: "기획안 또는 최종 결과를 확인하세요",
            tone: "amber",
          },
          {
            label: "실패 / 입력 필요",
            to: "/?state=attention",
            value: counts
              ? (counts.failed ?? 0) + (counts.needs_input ?? 0)
              : null,
            icon: GitBranch,
            note: "다음 조치가 필요한 작업",
            tone: "red",
          },
        ].map((item) => (
          <Link to={item.to} className="stat-card" key={item.label}>
            <div className="stat-label">
              {item.label}
              <item.icon size={17} />
            </div>
            <strong className={`stat-value text-${item.tone}`}>
              {item.value ?? "—"}
            </strong>
            <p>{item.note}</p>
          </Link>
        ))}
      </div>
      <section className="panel">
        <div className="panel-heading">
          <div>
            <h2>
              <Workflow size={18} />
              작업 목록
            </h2>
            <p className="subtle text-small">
              요청일시가 최신인 순서로 표시합니다.
            </p>
          </div>
          <select
            aria-label="작업 상태 필터"
            value={selected}
            onChange={(e) =>
              setParams(e.target.value ? { state: e.target.value } : {})
            }
          >
            <option value="">모든 상태</option>
            <option value="running">실행 중 전체</option>
            <option value="approvals">승인 대기 전체</option>
            <option value="attention">실패 / 입력 필요</option>
            {Object.entries(states).map(([key, value]) => (
              <option key={key} value={key}>
                {value.label}
              </option>
            ))}
          </select>
        </div>
        {runs.isPending ? (
          <Loading />
        ) : runs.error ? (
          <ErrorNotice error={runs.error} retry={() => runs.refetch()} />
        ) : (
          <RunsTable runs={runs.data.items} />
        )}
        <div className="panel-footer">
          <span className="subtle text-small">
            5초마다 상태를 새로 확인합니다.
          </span>
          <div className="inline-actions">
            {cursor && (
              <Button variant="ghost" onClick={() => showPage()}>
                처음으로
              </Button>
            )}
            {runs.data?.has_more && nextCursor && (
              <Button variant="secondary" onClick={() => showPage(nextCursor)}>
                다음 작업
                <ArrowUpRight size={14} />
              </Button>
            )}
          </div>
        </div>
      </section>
      <div className="workspace-note">
        <ShieldCheck size={17} />
        <p>
          각 작업은 격리된 Git Worktree에서 실행됩니다. 최종 승인에는 검증된
          커밋 확인이 필요합니다.
        </p>
      </div>
    </>
  );
}
