import type { Log } from "../hooks/events";

// Reassemble only contiguous ranges from the same immutable log generation.
export function logLines(logs: Log[]) {
  const rows: (Log & { key: string })[] = [];
  const tails = new Map<string, number>();
  for (const log of logs) {
    const identity =
      log.logId && typeof log.generation === "number"
        ? `${log.logId}:${log.generation}`
        : undefined;
    const previousIndex = identity ? tails.get(identity) : undefined;
    const previous =
      previousIndex === undefined ? undefined : rows[previousIndex];
    if (
      previous &&
      typeof previous.endOffset === "number" &&
      previous.endOffset === log.offset &&
      !previous.text.endsWith("\n")
    ) {
      previous.text += log.text;
      previous.endOffset = log.endOffset;
      previous.id = log.id;
    } else {
      rows.push({ ...log, key: `${log.id}` });
      if (identity) tails.set(identity, rows.length - 1);
    }
  }
  return rows;
}
export function describeLog(
  text: string,
): { label: string; body: string } | null {
  if (!text.endsWith("\n")) return null;
  try {
    const event = JSON.parse(text);
    if (!event || typeof event !== "object") return null;
    const item = event.item;
    if (item?.type === "agent_message" && typeof item.text === "string")
      return { label: "메시지", body: item.text };
    if (item?.type === "command_execution" && typeof item.command === "string")
      return {
        label:
          item.exit_code == null
            ? "명령 실행"
            : `명령 종료 · 코드 ${item.exit_code}`,
        body: [
          item.command,
          typeof item.aggregated_output === "string"
            ? item.aggregated_output
            : "",
        ]
          .filter(Boolean)
          .join("\n"),
      };
    if (
      (event.type === "error" || event.type === "turn.failed") &&
      typeof (event.message ?? event.error?.message) === "string"
    )
      return { label: "오류", body: event.message ?? event.error.message };
    if (item?.type === "file_change" && Array.isArray(item.changes))
      return {
        label: "파일 변경",
        body: item.changes
          .filter(
            (c: unknown) =>
              c &&
              typeof c === "object" &&
              "path" in c &&
              typeof c.path === "string",
          )
          .map((c: { path: string }) => c.path)
          .join("\n"),
      };
    return null;
  } catch {
    return null;
  }
}
