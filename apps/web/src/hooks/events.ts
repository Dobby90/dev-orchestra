import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { api } from "../api/resources";
import type { Event as RunEvent } from "../api/types";
export type Log = {
  id: number;
  text: string;
  role: string | null;
  level: string;
  source: string;
  step: number | null;
  logId?: string;
  generation?: number;
  offset?: number;
  endOffset?: number;
};
export function eventLog(event: RunEvent): Log | null {
  const d = event.data;
  if (
    event.kind !== "log.appended" ||
    typeof d.text !== "string" ||
    typeof d.source !== "string"
  )
    return null;
  return {
    logId: typeof d.log_id === "string" ? d.log_id : undefined,
    generation: typeof d.generation === "number" ? d.generation : undefined,
    offset: typeof d.offset === "number" ? d.offset : undefined,
    endOffset: typeof d.end_offset === "number" ? d.end_offset : undefined,
    id: event.id,
    text: d.text,
    source: d.source,
    role: typeof d.role === "string" ? d.role : null,
    level: typeof d.level === "string" ? d.level : "unknown",
    step: typeof d.step_id === "number" ? d.step_id : null,
  };
}
export type Connection = "connecting" | "live" | "reconnecting";
const kinds = [
  "state.changed",
  "issue.assessed",
  "issue.disposition",
  "changes.requested",
  "test_changes.proposed",
  "test_changes.decided",
  "test_changes.applied",
  "approval.deferred",
  "operation.accepted",
  "operation.applied",
  "step.interrupted",
  "control.requested",
  "step.started",
  "step.completed",
  "approval.recorded",
  "plan.decided",
  "job.queued",
  "job.claimed",
  "job.running",
  "job.finished",
  "job.recovered",
  "log.appended",
  "log.unavailable",
  "event.unavailable",
];
export function decodeEvent(value: unknown, rid: string): RunEvent | null {
  if (!value || typeof value !== "object") return null;
  const e = value as Record<string, unknown>;
  if (
    e.run_id !== rid ||
    typeof e.id !== "number" ||
    !Number.isSafeInteger(e.id) ||
    e.id < 1 ||
    typeof e.kind !== "string" ||
    typeof e.data !== "object" ||
    !e.data ||
    Array.isArray(e.data) ||
    e.schema_version !== 1 ||
    typeof e.occurred_at !== "string"
  )
    return null;
  return e as RunEvent;
}
export function appendBounded(previous: Log[], incoming: Log[]) {
  const items = [...previous, ...incoming];
  let bytes = items.reduce(
      (n, e) => n + new TextEncoder().encode(e.text).length,
      0,
    ),
    removed = 0;
  while (items.length > 1000 || bytes > 1048576) {
    bytes -= new TextEncoder().encode(items.shift()!.text).length;
    removed++;
  }
  return { items, removed };
}
export function useRunEvents(rid: string) {
  const client = useQueryClient();
  const [connection, setConnection] = useState<Connection>("connecting");
  const [buffer, setBuffer] = useState<{ items: Log[]; removed: number }>({
    items: [],
    removed: 0,
  });
  const [unavailable, setUnavailable] = useState(false);
  const cursor = useRef(0);
  const [lastReceived, setLastReceived] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [hasOlder, setHasOlder] = useState(false);
  const [beforeCursor, setBeforeCursor] = useState<number>();
  useEffect(() => {
    let disposed = false,
      busy = false,
      current: Connection = "connecting";
    const pending: Log[] = [];
    let pendingReceived: string | null = null;
    let pendingBytes = 0,
      pendingRemoved = 0;
    let invalidate: ReturnType<typeof setTimeout> | undefined;
    const change = (value: Connection) => {
      current = value;
      if (!disposed) setConnection(value);
    };
    function consume(raw: unknown) {
      const event = decodeEvent(raw, rid);
      if (disposed || !event || event.id <= cursor.current) return;
      cursor.current = event.id;
      pendingReceived = new Date().toISOString();
      if (event.kind === "log.appended") {
        const d = event.data;
        if (typeof d.text === "string" && typeof d.source === "string") {
          pending.push(eventLog(event)!);
          pendingBytes += new TextEncoder().encode(d.text).length;
          while (pending.length > 1000 || pendingBytes > 1048576) {
            pendingBytes -= new TextEncoder().encode(
              pending.shift()!.text,
            ).length;
            pendingRemoved++;
          }
        }
      } else {
        if (event.kind.endsWith(".unavailable")) setUnavailable(true);
        if (!invalidate)
          invalidate = setTimeout(() => {
            invalidate = undefined;
            if (!disposed) {
              client.invalidateQueries({ queryKey: ["run", rid] });
              client.invalidateQueries({ queryKey: ["steps", rid] });
              client.invalidateQueries({ queryKey: ["runs"] });
              client.invalidateQueries({ queryKey: ["dashboard"] });
              ["tests", "reviews", "changes", "report"].forEach((key) =>
                client.invalidateQueries({ queryKey: [key, rid] }),
              );
            }
          }, 200);
      }
    }
    let stream: EventSource | undefined;
    let initialized = false;
    async function initialize() {
      if (busy || initialized || disposed) return;
      busy = true;
      try {
        const page = await api.logWindow(rid);
        if (disposed) return;
        page.items.forEach(consume);
        cursor.current = page.latest_cursor;
        setHasOlder(page.has_more);
        setBeforeCursor(page.before_cursor);
        setLoading(false);
        setLoadError(false);
        initialized = true;
        stream = new EventSource(
          `/api/runs/${encodeURIComponent(rid)}/events?after=${cursor.current}`,
          { withCredentials: true },
        );
        stream.onopen = () => change("live");
        stream.onerror = () => change("reconnecting");
        const listener = (event: MessageEvent) => {
          try {
            consume(JSON.parse(event.data));
          } catch {
            setUnavailable(true);
          }
        };
        kinds.forEach((kind) =>
          stream!.addEventListener(kind, listener as EventListener),
        );
      } catch {
        if (!disposed) {
          setLoading(false);
          setLoadError(true);
          change("reconnecting");
        }
      } finally {
        busy = false;
      }
    }
    void initialize();
    const flush = setInterval(() => {
      if (pendingReceived && !disposed) {
        setLastReceived(pendingReceived);
        pendingReceived = null;
      }
      if (pending.length && !disposed) {
        const batch = pending.splice(0),
          dropped = pendingRemoved;
        pendingBytes = 0;
        pendingRemoved = 0;
        setBuffer((previous) => {
          const result = appendBounded(previous.items, batch);
          return {
            items: result.items,
            removed: previous.removed + result.removed + dropped,
          };
        });
      }
    }, 100);
    const fallback = setInterval(async () => {
      if (!initialized) {
        void initialize();
        return;
      }
      if (busy || disposed || current === "live") return;
      busy = true;
      try {
        await api.session();
        for (let pages = 0; pages < 5 && !disposed; pages++) {
          const page = await api.history(rid, cursor.current);
          page.items.forEach(consume);
          if (!page.has_more || !page.items.length) break;
        }
      } catch {
        /* The shared API client invalidates an expired session. */
      } finally {
        busy = false;
      }
    }, 3000);
    return () => {
      disposed = true;
      stream?.close();
      clearInterval(flush);
      clearInterval(fallback);
      clearTimeout(invalidate);
    };
  }, [rid, client]);
  return {
    connection,
    lastReceived,
    loading,
    loadError,
    hasOlder,
    beforeCursor,
    logs: buffer.items,
    removed: buffer.removed,
    unavailable,
  };
}
