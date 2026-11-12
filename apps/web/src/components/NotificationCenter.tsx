import { useEffect, useRef, useState } from "react";
import {
  useInfiniteQuery,
  useMutation,
  useQueryClient,
} from "@tanstack/react-query";
import { Link, useLocation } from "react-router-dom";
import { Bell } from "lucide-react";
import { api } from "../api/resources";
import { ErrorNotice, Loading, Status } from "./common";
import { Button } from "./ui/button";
import { Modal } from "./ui/dialog";

export function NotificationCenter() {
  const [open, setOpen] = useState(false);
  const [message, setMessage] = useState("");
  const client = useQueryClient();
  const location = useLocation();
  const cursor = useRef<number | null>(null);
  const previousPath = useRef(location.pathname);
  const query = useInfiniteQuery({
    queryKey: ["notifications"],
    queryFn: ({ pageParam }) => api.notifications(pageParam),
    initialPageParam: undefined as number | undefined,
    getNextPageParam: (page) => page.next_cursor ?? undefined,
    refetchInterval: 3000,
    refetchIntervalInBackground: true,
  });
  const page = query.data?.pages[0];
  useEffect(() => {
    if (!page) return;
    const previous = cursor.current;
    const navigated = previousPath.current !== location.pathname;
    previousPath.current = location.pathname;
    cursor.current = page.cursor;
    // Existing notifications remain in the list without alerting on entry/reload.
    if (previous === null || navigated) return;
    const notification = page.items.find(
      (item) =>
        item.id > previous && location.pathname === `/runs/${item.run_id}`,
    );
    if (notification) {
      client.invalidateQueries({ queryKey: ["run", notification.run_id] });
      setMessage(`${notification.run_id} · ${notification.message}`);
    }
  }, [page, location.pathname, client]);
  const read = useMutation({
    mutationFn: api.readNotifications,
    onSuccess: () => client.invalidateQueries({ queryKey: ["notifications"] }),
  });
  const items = Array.from(
    new Map(
      query.data?.pages.flatMap((p) => p.items).map((item) => [item.id, item]),
    ),
  )
    .map(([, item]) => item)
    .filter((item) => !item.read)
    .sort((a, b) => b.id - a.id);
  const count = page?.unread_count ?? 0;
  return (
    <>
      {message && (
        <div className="notification-toast" role="status">
          <p>{message}</p>
          <Button
            variant="secondary"
            onClick={() => {
              setOpen(true);
              setMessage("");
            }}
          >
            알림 확인
          </Button>
          <Button
            variant="ghost"
            aria-label="상태 알림 닫기"
            onClick={() => setMessage("")}
          >
            닫기
          </Button>
        </div>
      )}
      <Modal
        open={open}
        onOpenChange={setOpen}
        title="알림"
        description="입력 필요, 일시중지, 승인 대기와 실패 알림을 확인하세요. 읽은 알림은 목록에서 사라지며 작업 상태는 변경되지 않습니다."
        trigger={
          <button
            className="icon-button notification-bell"
            aria-label={`알림, 읽지 않음 ${count} 개${count > 99 ? " (99+)" : ""}`}
          >
            <Bell size={20} />
            {count > 0 && (
              <span className="notification-count">
                {count > 99 ? "99+" : count}
              </span>
            )}
            {query.isError && <span className="notification-count">!</span>}
          </button>
        }
      >
        {query.isPending && <Loading />}
        {query.error && (
          <ErrorNotice error={query.error} retry={() => query.refetch()} />
        )}
        {read.error && <ErrorNotice error={read.error} />}
        <div className="notification-toolbar">
          <span className="subtle">읽지 않은 알림 {count}개</span>
          <Button
            variant="secondary"
            disabled={!count || read.isPending}
            onClick={() => read.mutate({ through: page?.cursor })}
          >
            모두 읽음
          </Button>
        </div>
        <ul className="notification-list">
          {items.map((item) => (
            <li
              key={item.id}
              className={item.read ? "" : "notification-unread"}
            >
              <Link
                to={`/runs/${item.run_id}`}
                onClick={() => {
                  if (!item.read) read.mutate({ ids: [item.id] });
                  setOpen(false);
                }}
              >
                <div className="notification-row">
                  <strong>{item.run_id}</strong>
                  <Status state={item.state} />
                </div>
                <p>{item.message}</p>
                <time dateTime={item.occurred_at}>
                  {new Date(item.occurred_at).toLocaleString("ko-KR")}
                </time>
                {!item.read && <span className="sr-only">읽지 않음</span>}
              </Link>
            </li>
          ))}
        </ul>
        {query.isSuccess && !items.length && (
          <p className="subtle">읽지 않은 알림이 없습니다.</p>
        )}
        {query.hasNextPage && (
          <Button
            variant="secondary"
            disabled={query.isFetchingNextPage}
            onClick={() => query.fetchNextPage()}
          >
            이전 알림 더 보기
          </Button>
        )}
      </Modal>
    </>
  );
}
