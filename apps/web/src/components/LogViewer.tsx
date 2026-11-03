import { Link } from "react-router-dom";
import { useEffect, useMemo, useRef, useState } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { ArrowDownToLine, Download, Pause, Search } from "lucide-react";
import {
  appendBounded,
  eventLog,
  type Connection,
  type Log,
} from "../hooks/events";
import { api } from "../api/resources";
import type { RunDetail, Step } from "../api/types";
import { logLines, describeLog } from "./logPresentation";
import { roles, Empty } from "./common";
import { Button } from "./ui/button";
export function LogViewer({
  logs,
  removed,
  unavailable,
  runId,
  stepId,
  steps = [],
  state,
  connection = "connecting",
  lastReceived,
  loading = false,
  loadError = false,
  hasOlder = false,
  beforeCursor,
}: {
  logs: Log[];
  removed: number;
  unavailable: boolean;
  runId?: string;
  stepId?: number;
  steps?: Step[];
  state?: RunDetail["state"];
  connection?: Connection;
  lastReceived?: string | null;
  loading?: boolean;
  loadError?: boolean;
  hasOlder?: boolean;
  beforeCursor?: number;
}) {
  const [role, setRole] = useState(""),
    [level, setLevel] = useState(""),
    [query, setQuery] = useState(""),
    [follow, setFollow] = useState(true);
  const parent = useRef<HTMLDivElement>(null);
  const dragging = useRef(false);
  const [history, setHistory] = useState<Log[] | null>(null);
  const [historyCursor, setHistoryCursor] = useState<number>();
  const [older, setOlder] = useState(hasOlder);
  const [historyUnavailable, setHistoryUnavailable] = useState(false);
  const [historyError, setHistoryError] = useState(false);
  const [fetching, setFetching] = useState(false);
  const [seen, setSeen] = useState(0);

  const unseen = follow ? 0 : Math.max(0, removed + logs.length - seen);
  async function loadOlder() {
    if (!runId || fetching) return;
    setFetching(true);
    setHistoryError(false);
    try {
      const before =
        historyCursor ?? (stepId ? undefined : (logs[0]?.id ?? beforeCursor));
      const page = await api.logWindow(runId, before, stepId);
      const incoming = page.items
        .map(eventLog)
        .filter((log): log is Log => log !== null);
      const existing = history ?? (stepId ? [] : logs);
      const combined = [
        ...new Map(
          [...incoming, ...existing].map((log) => [log.id, log]),
        ).values(),
      ].sort((a, b) => a.id - b.id);
      // Keep the older end while browsing history; live reception stays bounded separately.
      const bounded = appendBounded([], combined.reverse()).items.reverse();
      setHistory(bounded);
      setHistoryCursor(page.before_cursor);
      setOlder(page.has_more);
      setHistoryUnavailable(
        (value) =>
          value || page.items.some((e) => e.kind.endsWith(".unavailable")),
      );
      setFollow(false);
      setSeen(removed + logs.length);
    } catch {
      setHistoryError(true);
    } finally {
      setFetching(false);
    }
  }
  function latest() {
    setHistory(null);
    setHistoryCursor(undefined);
    setFollow(true);
    setSeen(removed + logs.length);
  }
  function stopFollowing() {
    if (history === null) {
      setHistory(logs.filter((log) => !stepId || log.step === stepId));
      setHistoryCursor(logs[0]?.id);
      setOlder(hasOlder || removed > 0);
    }
    setFollow(false);
    setSeen(removed + logs.length);
  }
  useEffect(() => {
    if (stepId && !loading) void loadOlder();
  }, [stepId, loading]);
  const filtered = useMemo(
    () =>
      logLines(history ?? logs).filter(
        (log) =>
          (!stepId || log.step === stepId) &&
          (!role || log.role === role) &&
          (!level || log.level === level) &&
          (!query || log.text.toLowerCase().includes(query.toLowerCase())),
      ),
    [logs, history, role, level, query, stepId],
  );
  const virtual = useVirtualizer({
    count: filtered.length,
    getItemKey: (index) => filtered[index].key,
    getScrollElement: () => parent.current,
    estimateSize: () => 54,
    overscan: 8,
  });
  useEffect(() => {
    if (follow && filtered.length)
      virtual.scrollToIndex(filtered.length - 1, { align: "end" });
  }, [filtered.length, filtered.at(-1)?.id, follow, virtual]);
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
          {(() => {
            const step = steps.find((s) => s.id === stepId);
            return step
              ? `revision ${step.revision ?? 0} · 회차 ${step.iteration} · 시도 ${step.attempt} · ${roles[step.role] ?? step.role}`
              : `단계 ${stepId}`;
          })()}{" "}
          로그 · <Link to="?tab=logs">전체 로그 보기</Link>
        </p>
      )}
      <p role="status" className="help">
        {connection === "live"
          ? "실시간 연결됨"
          : connection === "connecting"
            ? "연결 중…"
            : "재연결 중 · 주기적 조회"}
        {lastReceived && (
          <>
            {" "}
            · 마지막 수신{" "}
            <time dateTime={lastReceived}>
              {new Date(lastReceived).toLocaleTimeString("ko-KR")}
            </time>
          </>
        )}
      </p>
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
          onClick={() => (follow ? stopFollowing() : latest())}
        >
          {follow ? <Pause size={15} /> : <ArrowDownToLine size={15} />}자동
          스크롤 {follow ? "중지" : "재개"}
        </Button>
        <Button variant="ghost" disabled={!filtered.length} onClick={download}>
          <Download size={15} />
          화면 로그 저장
        </Button>
      </div>
      {(history !== null || !follow) && (
        <Button variant="ghost" onClick={latest}>
          <ArrowDownToLine size={15} /> 새 로그 {unseen}개 · 최신으로
        </Button>
      )}
      {runId &&
        (history !== null ? older : hasOlder || removed > 0 || !!stepId) && (
          <Button variant="ghost" disabled={fetching} onClick={loadOlder}>
            {fetching ? "이전 로그 조회 중…" : "이전 로그 더 보기"}
          </Button>
        )}
      {historyError && (
        <p role="alert">이전 로그를 불러오지 못했습니다. 다시 시도해 주세요.</p>
      )}
      {loadError && (
        <p role="alert">
          로그를 불러오지 못했습니다. 자동으로 다시 시도합니다.
        </p>
      )}
      <p className="help">
        화면에는 최대 1,000개 구간·1 MiB를 보관합니다. 검색·저장은 이 범위에
        적용됩니다.
        {history === null &&
          removed > 0 &&
          ` 이전 ${removed}개 구간은 화면에서 제외되었습니다.`}{" "}
        {history !== null &&
          " 과거 로그를 보는 중입니다. 최신 수신은 계속됩니다."}
        원본 이력은 서버에 보존됩니다.
      </p>
      {(unavailable || historyUnavailable) && (
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
          onWheel={(event) => {
            if (event.deltaY < 0 && follow) stopFollowing();
          }}
          onTouchMove={() => {
            if (follow) stopFollowing();
          }}
          onKeyDown={(event) => {
            if (["ArrowUp", "PageUp", "Home"].includes(event.key) && follow)
              stopFollowing();
          }}
          onPointerDown={() => {
            dragging.current = true;
          }}
          onPointerUp={() => {
            dragging.current = false;
          }}
          onPointerCancel={() => {
            dragging.current = false;
          }}
          onScroll={(event) => {
            const el = event.currentTarget;
            if (
              follow &&
              dragging.current &&
              el.scrollTop + el.clientHeight < el.scrollHeight - 64
            )
              stopFollowing();
          }}
        >
          <div style={{ height: virtual.getTotalSize(), position: "relative" }}>
            {virtual.getVirtualItems().map((row) => {
              const log = filtered[row.index];
              return (
                <div
                  key={log.key}
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
                    {(() => {
                      const step = steps.find((s) => s.id === log.step);
                      return step
                        ? ` · revision ${step.revision ?? 0} · 회차 ${step.iteration} · 시도 ${step.attempt}`
                        : log.step
                          ? ` · 단계 ${log.step}`
                          : "";
                    })()}
                  </span>
                  <LogBody text={log.text} />
                </div>
              );
            })}
          </div>
        </div>
      ) : (
        <Empty
          title={
            loading
              ? "로그를 불러오고 있습니다"
              : loadError
                ? "로그 조회 실패"
                : unavailable || historyUnavailable
                  ? "로그를 읽을 수 없습니다"
                  : logs.length ||
                      history?.length ||
                      query ||
                      role ||
                      level ||
                      stepId
                    ? "필터 결과가 없습니다"
                    : state &&
                        ["completed", "cancelled", "failed"].includes(state)
                      ? "저장된 로그가 없습니다"
                      : state === "queued"
                        ? "실행을 기다리고 있습니다"
                        : "로그를 기다리고 있습니다"
          }
        >
          {state && ["completed", "cancelled", "failed"].includes(state)
            ? "작업이 종료되었습니다. 필터와 저장된 이력을 확인해 주세요."
            : "새 로그가 기록되면 자동으로 표시됩니다."}
        </Empty>
      )}
    </section>
  );
}

function LogBody({ text }: { text: string }) {
  const description = describeLog(text);
  return (
    <>
      {description && <strong className="log-kind">{description.label}</strong>}
      {text.length > 1200 || text.split("\n").length > 16 ? (
        <details>
          <summary>긴 출력 보기</summary>
          <pre>{description?.body ?? text}</pre>
        </details>
      ) : (
        <pre>{description?.body ?? text}</pre>
      )}
      {description && (
        <details>
          <summary>원문 보기</summary>
          <pre>{text}</pre>
        </details>
      )}
    </>
  );
}
