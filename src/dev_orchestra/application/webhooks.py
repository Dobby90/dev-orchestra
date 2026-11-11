"""Durable workspace webhook delivery, independent of browser reads and Run execution."""

import json
import threading
import time
import uuid
from contextlib import contextmanager

from dev_orchestra.application.errors import ApplicationError
from dev_orchestra.application.notifications import MESSAGES, TRANSITIONS
from dev_orchestra.infrastructure.files import write
from dev_orchestra.infrastructure.locking import OwnershipError, lock
from dev_orchestra.infrastructure.store import now
from dev_orchestra.infrastructure.webhooks import APPS, WebhookError, send, validate_url


def message(event):
    title, description = MESSAGES[event["state"]]
    # Run IDs are identifiers, never a source of Markdown, mentions or extra lines.
    run_id = "".join(c for c in event["run_id"] if c.isalnum() or c in "-_.")[:200]
    return (
        f"[Dev Orchestra][{title}]\n작업: {run_id}\n{description}\n"
        f"발생 시각: {event['occurred_at']}\n알림 ID: {event['id']}"
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
                "[Dev Orchestra][연결 테스트]\n웹훅 테스트 알림입니다. 실제 작업 상태는 변경되지 않습니다.",
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
                pending = saved["pending"]
                if pending is None:
                    with self.store.connection() as db:
                        event = db.execute(
                            TRANSITIONS + " AND id>? ORDER BY id LIMIT 1", (saved["cursor"],)
                        ).fetchone()
                    if event is None:
                        return
                    pending = {"event": dict(event), "attempts": 0, "retry_at": 0}
                if pending["retry_at"] > time.time():
                    return
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
