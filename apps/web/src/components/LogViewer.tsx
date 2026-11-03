import { Link } from "react-router-dom";
import { useEffect, useMemo, useRef, useState } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { ArrowDownToLine, Download, Pause, Search } from "lucide-react";
import type { Log } from "../hooks/events";
import { roles, Empty } from "./common";
import { Button } from "./ui/button";
export function LogViewer({
  logs,
  removed,
  unavailable,
  runId,
  stepId,
}: {
  logs: Log[];
  removed: number;
  unavailable: boolean;
  runId?: string;
  stepId?: number;
}) {
  const [role, setRole] = useState(""),
    [level, setLevel] = useState(""),
    [query, setQuery] = useState(""),
    [follow, setFollow] = useState(true);
  const parent = useRef<HTMLDivElement>(null);
  const filtered = useMemo(
    () =>
      logs.filter(
        (log) =>
          (!stepId || log.step === stepId) &&
          (!role || log.role === role) &&
          (!level || log.level === level) &&
          (!query || log.text.toLowerCase().includes(query.toLowerCase())),
      ),
    [logs, role, level, query, stepId],
  );
  const virtual = useVirtualizer({
    count: filtered.length,
    getScrollElement: () => parent.current,
    estimateSize: () => 54,
    overscan: 8,
  });
  useEffect(() => {
    if (follow && filtered.length)
      virtual.scrollToIndex(filtered.length - 1, { align: "end" });
  }, [filtered.length, follow, virtual]);
  function download() {
    const url = URL.createObjectURL(
      new Blob([filtered.map((l) => `[${l.source}] ${l.text}`).join("")], {
        type: "text/plain;charset=utf-8",
      }),
    );
    const link = document.createElement("a");
    link.href = url;
    link.download = "devorchestra-visible-logs.txt";
    link.click();
    URL.revokeObjectURL(url);
  }
  return (
    <section>
      {stepId && (
        <p role="status">
          단계 {stepId} 로그 · <Link to="?tab=logs">전체 로그 보기</Link>
        </p>
      )}
      <div className="log-toolbar">
        {runId && (
          <Button asChild variant="ghost">
            <a href={`/api/runs/${encodeURIComponent(runId)}/logs/download`}>
              <Download size={15} />
              서버 로그 다운로드
            </a>
          </Button>
        )}
        <select
          aria-label="로그 에이전트"
          value={role}
          onChange={(e) => setRole(e.target.value)}
        >
          <option value="">전체 에이전트</option>
          {Object.entries(roles).map(([key, value]) => (
            <option key={key} value={key}>
              {value}
            </option>
          ))}
        </select>
        <select
          aria-label="로그 레벨"
          value={level}
          onChange={(e) => setLevel(e.target.value)}
        >
          <option value="">전체 레벨</option>
          {["debug", "info", "warning", "error", "critical", "unknown"].map(
            (value) => (
              <option key={value}>{value}</option>
            ),
          )}
        </select>
        <label className="search">
          <Search size={15} />
          <input
            aria-label="화면 로그 검색"
            placeholder="현재 보관된 로그 검색"
            value={query}
            maxLength={200}
            onChange={(e) => setQuery(e.target.value)}
          />
        </label>
        <Button
          variant="ghost"
          aria-pressed={follow}
          onClick={() => setFollow(!follow)}
        >
          {follow ? <Pause size={15} /> : <ArrowDownToLine size={15} />}자동
          스크롤 {follow ? "중지" : "재개"}
        </Button>
        <Button variant="ghost" disabled={!filtered.length} onClick={download}>
          <Download size={15} />
          화면 로그 저장
        </Button>
      </div>
      <p className="help">
        화면에는 최근 최대 1,000개 구간·1 MiB를 보관합니다. 검색·저장은 이
        범위에 적용됩니다.
        {removed > 0 &&
          ` 이전 ${removed}개 구간은 화면에서 제외되었습니다.`}{" "}
        원본 이력은 서버에 보존됩니다.
      </p>
      {unavailable && (
        <p role="status" className="notice">
          일부 로그를 읽을 수 없습니다. 변조되거나 삭제된 구간을 정상 로그로
          표시하지 않습니다.
        </p>
      )}
      {filtered.length ? (
        <div
          className="log-viewport"
          ref={parent}
          tabIndex={0}
          aria-label="실행 로그"
        >
          <div style={{ height: virtual.getTotalSize(), position: "relative" }}>
            {virtual.getVirtualItems().map((row) => {
              const log = filtered[row.index];
              return (
                <div
                  key={log.id}
                  data-index={row.index}
                  ref={virtual.measureElement}
                  className="log-row"
                  style={{
                    position: "absolute",
                    width: "100%",
                    transform: `translateY(${row.start}px)`,
                  }}
                >
                  <span className="log-meta">
                    #{log.id} ·{" "}
                    {log.role ? (roles[log.role] ?? log.role) : log.source} ·{" "}
                    {log.level}
                    {log.step ? ` · 단계 ${log.step}` : ""}
                  </span>
                  <pre>{log.text}</pre>
                </div>
              );
            })}
          </div>
        </div>
      ) : (
        <Empty
          title={
            logs.length ? "검색 결과가 없습니다" : "로그를 기다리고 있습니다"
          }
        >
          로그는 작업 진행에 따라 자동으로 표시됩니다.
        </Empty>
      )}
    </section>
  );
}
