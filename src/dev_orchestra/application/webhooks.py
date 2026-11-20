"""Durable workspace webhook delivery, independent of browser reads and Run execution."""

import json
import re
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

from dev_orchestra.application.errors import ApplicationError
from dev_orchestra.application.notification_events import MESSAGES, STATE_TRANSITIONS
from dev_orchestra.infrastructure.files import write
from dev_orchestra.infrastructure.locking import OwnershipError, lock
from dev_orchestra.infrastructure.store import now
from dev_orchestra.infrastructure.webhooks import APPS, WebhookError, send, validate_url

STAGES = {
    "planning": "기획",
    "developing": "개발",
    "testing": "테스트",
    "reviewing": "리뷰",
    "fixing": "수정",
    "reporting": "결과 정리",
}
STATE_LABELS = (
    STAGES
    | {"queued": "대기", "preparing": "준비"}
    | {key: value[0] for key, value in MESSAGES.items()}
)

# Compute previous state over the entire history before applying channel/cursor filters.
WEBHOOK_TRANSITIONS = (
    STATE_TRANSITIONS
    + """
SELECT t.id,t.run_id,t.timestamp AS occurred_at,t.state,t.previous,t.transition_summary,
 json_extract(r.data,'$.name') AS run_name
FROM transitions t LEFT JOIN runs r ON r.id=t.run_id
WHERE (t.previous IS NULL OR t.previous != t.state)
AND (t.state IN ('needs_input','paused','waiting_approval','waiting_plan_approval','failed')
 OR (t.id>? AND t.state IN ('planning','developing','testing','reviewing','fixing','reporting')))
AND t.id>? ORDER BY t.id LIMIT 1
"""
)


def display_name(value, fallback):
    # Keep human names while neutralizing Markdown, mentions, HTML and control characters.
    name = " ".join(str(value or fallback).split())
    name = "".join(c for c in name if c.isprintable())[:100]
    name = name.replace("@", "＠").replace("<", "＜").replace(">", "＞")
    return re.sub(r"([\\`*_{}\[\]()!|~])", r"\\\1", name) or fallback


def message(event):
    state = event["state"]
    if state in STAGES:
        title = "단계 전환"
        previous = STATE_LABELS.get(event.get("previous"), "시작")
        description = f"진행: {previous} → {STAGES[state]}"
        summary = json.loads(event.get("transition_summary") or "{}")
        if (counts := summary.get("test_cases")) is not None:
            description += (
                f"\n테스트 {counts['total']}건 · 통과 {counts['passed']}건 / "
                f"실패 {counts['failed']}건 / 건너뜀 {counts['skipped']}건"
            )
        if (counts := summary.get("review_issues")) is not None:
            issues = " / ".join(
                f"{severity.title()} {counts.get(severity, 0)}건"
                for severity in ("critical", "high", "medium", "low")
            )
            description += f"\n미해결 리뷰 이슈 · {issues}"
        if summary.get("test_failed"):
            description += "\n테스트 단계 실패"
    else:
        title, description = MESSAGES[state]
    run_id = "".join(c for c in event["run_id"] if c.isalnum() or c in "-_.")[:200]
    name = display_name(event.get("run_name"), run_id)
    occurred = datetime.fromisoformat(event["occurred_at"].replace("Z", "+00:00"))
    occurred = occurred.astimezone(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M:%S (KST)")
    return (
        f"[{title}]\n작업명: {name}\n작업 ID: {run_id}\n{description}\n"
        f"발생 시각: {occurred}\n알림 ID: {event['id']}"
    )


class WebhookService:
    def __init__(self, store):
        self.store = store
        self.path = store.path.with_suffix(".webhooks.json")
        self.lock_path = self.path.with_suffix(".lock")

    def _read(self):
        try:
            return json.loads(self.path.read_text())
        except FileNotFoundError:
            return {
                "version": "0" * 32,
                "enabled": False,
                "app": "mattermost",
                "url": None,
                "cursor": 0,
                "pending": None,
                "last_delivery": None,
            }

    @staticmethod
    def _view(saved):
        return {
            "version": saved["version"],
            "enabled": saved["enabled"],
            "app": saved["app"],
            "url_configured": bool(saved["url"]),
            "apps": [{"id": key, "label": app.label} for key, app in APPS.items()],
            "last_delivery": saved["last_delivery"],
        }

    def get(self):
        return self._view(self._read())

    def _version(self, saved, expected):
        if saved["version"] != expected:
            raise ApplicationError("VERSION_CONFLICT", "웹훅 설정이 변경되었습니다.")

    def save(self, body):
        with lock(self.lock_path, wait=True):
            saved = self._read()
            self._version(saved, body.expected_version)
            url = body.url.get_secret_value() if body.url is not None else saved["url"]
            if body.app not in APPS or (body.enabled and not url):
                raise ApplicationError("VALIDATION_ERROR", "앱과 웹훅 URL을 확인해 주세요.")
            if url:
                try:
                    validate_url(body.app, url)
                except WebhookError as error:
                    raise ApplicationError("VALIDATION_ERROR", str(error)) from None
            with self.store.connection() as db:
                cursor = db.execute("SELECT coalesce(max(id),0) FROM events").fetchone()[0]
            saved.update(
                version=uuid.uuid4().hex,
                enabled=body.enabled,
                app=body.app,
                url=url,
                cursor=cursor,
                stage_after=cursor,
                pending=None,
                last_delivery=None,
            )
            # Atomic write uses a private (0600) temporary file; secrets never enter Run snapshots.
            write(self.path, saved)
            return self._view(saved)

    def test(self, expected_version):
        with lock(self.lock_path, wait=True):
            saved = self._read()
            self._version(saved, expected_version)
            if not saved["url"]:
                raise ApplicationError("VALIDATION_ERROR", "웹훅 URL을 먼저 저장해 주세요.")
            self._deliver(
                saved,
                "[연결 테스트]\n웹훅 테스트 알림입니다. 실제 작업 상태는 변경되지 않습니다.",
                None,
                1,
            )
            write(self.path, saved)
            return self._view(saved)

    def _deliver(self, saved, text, event_id, attempts):
        try:
            send(saved["app"], saved["url"], text)
        except WebhookError as error:
            status, detail, retryable = "failed", str(error), error.retryable
        else:
            status, detail, retryable = "sent", "웹훅을 발송했습니다.", False
        saved["last_delivery"] = {
            "status": status,
            "message": detail,
            "occurred_at": now(),
            "event_id": event_id,
            "attempts": attempts,
        }
        return status == "sent", retryable

    def tick(self):
        # Only this private delivery lock is held during HTTP, never a DB or repository lock.
        try:
            with lock(self.lock_path):
                saved = self._read()
                if not saved["enabled"]:
                    return
                if "stage_after" not in saved:
                    # Enable new stage alerts from this upgrade onward without losing old alerts.
                    with self.store.connection() as db:
                        saved["stage_after"] = db.execute(
                            "SELECT coalesce(max(id),0) FROM events"
                        ).fetchone()[0]
                    write(self.path, saved)
                pending = saved["pending"]
                if pending is None:
                    with self.store.connection() as db:
                        event = db.execute(
                            WEBHOOK_TRANSITIONS,
                            (saved["stage_after"], saved["cursor"]),
                        ).fetchone()
                    if event is None:
                        return
                    pending = {"event": dict(event), "attempts": 0, "retry_at": 0}
                if pending["retry_at"] > time.time():
                    return
                if "run_name" not in pending["event"]:
                    # Upgrade an already pending legacy alert once, then keep its name on retry.
                    with self.store.connection() as db:
                        row = db.execute(
                            "SELECT json_extract(data,'$.name') FROM runs WHERE id=?",
                            (pending["event"]["run_id"],),
                        ).fetchone()
                    pending["event"]["run_name"] = row[0] if row else None
                pending["attempts"] += 1
                saved["pending"] = pending
                # Persist attempt before HTTP, allowing bounded recovery after a process crash.
                pending["retry_at"] = time.time() + 30
                write(self.path, saved)
                if pending["attempts"] > 3:
                    saved["last_delivery"] = {
                        "status": "failed",
                        "message": "재시도 한도에 도달했습니다. 수신 여부를 확인해 주세요.",
                        "occurred_at": now(),
                        "event_id": pending["event"]["id"],
                        "attempts": 3,
                    }
                    success, retryable = False, False
                else:
                    success, retryable = self._deliver(
                        saved,
                        message(pending["event"]),
                        pending["event"]["id"],
                        pending["attempts"],
                    )
                if success or not retryable or pending["attempts"] >= 3:
                    saved["cursor"] = pending["event"]["id"]
                    saved["pending"] = None
                else:
                    pending["retry_at"] = time.time() + (5 if pending["attempts"] == 1 else 30)
                write(self.path, saved)
        except OwnershipError:
            pass  # Another API/Dispatcher owns delivery; there is no duplicate sender.

    @contextmanager
    def worker(self):
        stopped = threading.Event()

        def work():
            while not stopped.is_set():
                try:
                    self.tick()
                except Exception:
                    # A delivery/storage failure must never change a Run or leak its endpoint.
                    pass
                stopped.wait(1)

        thread = threading.Thread(target=work, name="webhook-delivery", daemon=True)
        thread.start()
        try:
            yield
        finally:
            stopped.set()
            thread.join()
