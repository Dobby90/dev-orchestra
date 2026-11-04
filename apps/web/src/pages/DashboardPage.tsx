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
export function DashboardPage() {
  const [params, setParams] = useSearchParams();
  const selected = params.get("state") ?? "",
    cursor = params.get("cursor") ?? "";
  const state = selected in states ? (selected as State) : "";
  const query = new URLSearchParams({ limit: "30", cursor });
  if (state) query.set("state", state);
  const dashboard = useQuery({
    queryKey: ["dashboard"],
    queryFn: api.dashboard,
    refetchInterval: 5000,
  });
  const runs = useQuery({
    queryKey: ["runs", state, cursor],
    queryFn: () => api.runs(query),
    refetchInterval: 5000,
  });
  const counts = dashboard.data?.counts_by_state;
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
            value: dashboard.data?.project_count,
            icon: FolderGit2,
            note: "로컬 저장소",
            tone: "neutral",
          },
          {
            label: "실행 중",
            value: running,
            icon: Play,
            note: "에이전트 작업 진행",
            tone: "blue",
          },
          {
            label: "승인 대기",
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
            value: counts
              ? (counts.failed ?? 0) + (counts.needs_input ?? 0)
              : null,
            icon: GitBranch,
            note: "다음 조치가 필요한 작업",
            tone: "red",
          },
        ].map((item) => (
          <div className="stat-card" key={item.label}>
            <div className="stat-label">
              {item.label}
              <item.icon size={17} />
            </div>
            <strong className={`stat-value text-${item.tone}`}>
              {item.value ?? "—"}
            </strong>
            <p>{item.note}</p>
          </div>
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
              CLI와 GUI의 작업이 같은 상태 저장소에서 표시됩니다.
            </p>
          </div>
          <select
            aria-label="작업 상태 필터"
            value={state}
            onChange={(e) =>
              setParams(e.target.value ? { state: e.target.value } : {})
            }
          >
            <option value="">모든 상태</option>
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
              <Button
                variant="ghost"
                onClick={() => setParams(state ? { state } : {})}
              >
                처음으로
              </Button>
            )}
            {runs.data?.has_more && (
              <Button
                variant="secondary"
                onClick={() =>
                  setParams({
                    ...Object.fromEntries(params),
                    cursor: runs.data.next_cursor!,
                  })
                }
              >
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
