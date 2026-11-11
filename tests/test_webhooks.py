"""Webhook settings, durable delivery and transport isolation without external requests."""

import json
import threading
from unittest.mock import MagicMock

import pytest

from dev_orchestra.application.errors import ApplicationError
from dev_orchestra.application.notifications import NotificationService
from dev_orchestra.application.web_contracts import WebhookSettingsInput
from dev_orchestra.application.webhooks import MESSAGES, WebhookService
from dev_orchestra.infrastructure.locking import lock
from dev_orchestra.infrastructure.store import Store
from dev_orchestra.infrastructure.webhooks import WebhookError, send, validate_url

URL = "https://mattermost.example/hooks/example-test-key"


@pytest.fixture
def hooks(tmp_path):
    store = Store(tmp_path / "state.db")
    store.create({"id": "run"})
    return WebhookService(store)


def save(hooks, **values):
    return hooks.save(
        WebhookSettingsInput(
            **{
                "expected_version": hooks.get()["version"],
                "enabled": True,
                "app": "mattermost",
                "url": URL,
                **values,
            }
        )
    )


def test_settings_private_masked_versioned_and_not_in_run(hooks):
    assert not hooks.path.exists()
    assert hooks.get()["apps"] == [{"id": "mattermost", "label": "Mattermost"}]
    first = hooks.get()["version"]
    view = save(hooks)
    assert view["url_configured"]
    assert URL not in json.dumps(view)
    assert hooks.path.stat().st_mode & 0o777 == 0o600
    assert URL not in json.dumps(hooks.store.get("run"))
    assert save(hooks, url=None, enabled=False)["url_configured"]
    with pytest.raises(ApplicationError, match="설정이 변경"):
        save(hooks, expected_version=first)
    with hooks.store.connection() as db:
        assert db.execute("SELECT version FROM schema_version").fetchone()[0] == 2


@pytest.mark.parametrize("state", list(MESSAGES))
def test_each_existing_notification_delivered_once_with_safe_template(hooks, monkeypatch, state):
    sent = MagicMock()
    monkeypatch.setattr("dev_orchestra.application.webhooks.send", sent)
    save(hooks)
    # Synthetic durable events cover every alert without driving unrelated model workflows.
    with hooks.store.connection() as db:
        hooks.store._event(db, "run", "state", {"state": state})
        hooks.store._event(db, "run", "state", {"state": state})
    NotificationService(hooks.store).mark_read([], through=100)
    run = hooks.store.get("run")
    hooks.tick()
    WebhookService(hooks.store).tick()
    sent.assert_called_once()
    app, url, text = sent.call_args.args
    assert app == "mattermost" and url == URL
    assert MESSAGES[state][0] in text and MESSAGES[state][1] in text
    assert "작업: run" in text and "알림 ID:" in text
    assert hooks.get()["last_delivery"]["status"] == "sent"
    assert hooks.store.get("run") == run


def test_skip_history_disabled_and_new_transition_back_to_same_state(hooks, monkeypatch):
    sent = MagicMock()
    monkeypatch.setattr("dev_orchestra.application.webhooks.send", sent)
    hooks.store.update("run", "paused")
    save(hooks)
    hooks.tick()
    sent.assert_not_called()
    hooks.store.update("run", "queued")
    hooks.store.update("run", "paused")
    hooks.tick()
    sent.assert_called_once()
    save(hooks, enabled=False, url=None)
    hooks.store.update("run", "queued")
    hooks.store.update("run", "paused")
    hooks.tick()
    assert sent.call_count == 1
    save(hooks, url=None)
    hooks.tick()
    assert sent.call_count == 1


def test_retry_survives_restart_is_bounded_and_does_not_block_later_events(hooks, monkeypatch):
    clock = [1000]
    monkeypatch.setattr("dev_orchestra.application.webhooks.time.time", lambda: clock[0])
    sent = MagicMock(side_effect=WebhookError("연결 실패"))
    monkeypatch.setattr("dev_orchestra.application.webhooks.send", sent)
    save(hooks)
    hooks.store.update("run", "paused")
    hooks.tick()
    assert hooks.get()["last_delivery"]["attempts"] == 1
    hooks.tick()
    assert sent.call_count == 1
    clock[0] += 5
    WebhookService(hooks.store).tick()
    clock[0] += 30
    hooks.tick()
    hooks.tick()
    assert sent.call_count == 3
    assert hooks._read()["pending"] is None
    hooks.store.update("run", "queued")
    hooks.store.update("run", "paused")
    sent.side_effect = None
    hooks.tick()
    assert hooks.get()["last_delivery"]["status"] == "sent"
    assert sent.call_count == 4


def test_settings_change_drops_pending_and_rejects_stale_test(hooks, monkeypatch):
    sent = MagicMock(side_effect=WebhookError("연결 실패"))
    monkeypatch.setattr("dev_orchestra.application.webhooks.send", sent)
    before = save(hooks)
    hooks.store.update("run", "paused")
    hooks.tick()
    save(hooks, url="https://other.example/hooks/new-key")
    assert hooks._read()["pending"] is None
    hooks.tick()
    assert sent.call_count == 1
    with pytest.raises(ApplicationError):
        hooks.test(before["version"])


def test_test_message_and_permanent_failure_never_retry_or_change_run(hooks, monkeypatch):
    sent = MagicMock(side_effect=WebhookError("HTTP 400", retryable=False))
    monkeypatch.setattr("dev_orchestra.application.webhooks.send", sent)
    settings = save(hooks, enabled=False)
    assert hooks.test(settings["version"])["last_delivery"]["status"] == "failed"
    assert "연결 테스트" in sent.call_args.args[2]
    save(hooks)
    hooks.store.update("run", "paused")
    before = hooks.store.get("run")
    hooks.tick()
    hooks.tick()
    assert sent.call_count == 2
    assert hooks.store.get("run") == before


def test_competing_worker_skips_owned_delivery(hooks, monkeypatch):
    sent = MagicMock()
    monkeypatch.setattr("dev_orchestra.application.webhooks.send", sent)
    save(hooks)
    hooks.store.update("run", "paused")
    with lock(hooks.lock_path):
        WebhookService(hooks.store).tick()
    sent.assert_not_called()
    hooks.tick()
    sent.assert_called_once()


def test_crash_after_send_recovers_with_same_event_and_bounded_attempts(hooks, monkeypatch):
    from dev_orchestra.infrastructure.files import write as atomic_write

    sent = MagicMock()
    monkeypatch.setattr("dev_orchestra.application.webhooks.send", sent)
    clock = [1000]
    monkeypatch.setattr("dev_orchestra.application.webhooks.time.time", lambda: clock[0])
    save(hooks)
    hooks.store.update("run", "paused")
    writes = [0]

    def crash_on_receipt(path, data):
        writes[0] += 1
        if writes[0] % 2 == 0:
            raise OSError("simulated process interruption after HTTP")
        return atomic_write(path, data)

    monkeypatch.setattr("dev_orchestra.application.webhooks.write", crash_on_receipt)
    for _ in range(3):
        with pytest.raises(OSError):
            WebhookService(hooks.store).tick()
        clock[0] += 30
    assert sent.call_count == 3
    assert len({call.args[2] for call in sent.call_args_list}) == 1
    monkeypatch.setattr("dev_orchestra.application.webhooks.write", atomic_write)
    WebhookService(hooks.store).tick()
    assert sent.call_count == 3
    assert hooks._read()["pending"] is None
    assert hooks.get()["last_delivery"]["attempts"] == 3


def test_http_runs_without_database_transaction_and_receives_no_sensitive_payload(
    hooks, monkeypatch
):
    save(hooks)
    hooks.store.update("run", "paused", questions=["PRIVATE QUESTION"], error="PRIVATE ERROR")

    def receive(app, url, text):
        assert "PRIVATE" not in text and URL not in text
        with hooks.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("SELECT id FROM runs").fetchall()

    monkeypatch.setattr("dev_orchestra.application.webhooks.send", receive)
    hooks.tick()
    assert hooks.get()["last_delivery"]["status"] == "sent"


def test_background_worker_delivers_and_stops(hooks, monkeypatch):
    delivered = threading.Event()
    monkeypatch.setattr("dev_orchestra.application.webhooks.send", lambda *args: delivered.set())
    save(hooks)
    hooks.store.update("run", "paused")
    with hooks.worker():
        assert delivered.wait(3)
    assert not any(t.name == "webhook-delivery" for t in threading.enumerate())


@pytest.mark.parametrize(
    "url",
    [
        "http://mattermost.example/hooks/key",
        "https://user:secret@mattermost.example/hooks/key",
        "https://mattermost.example/hooks/key?token=secret",
        "https://mattermost.example/hooks/key#secret",
        "https://localhost/hooks/key",
        "https://127.0.0.1/hooks/key",
        "https://169.254.169.254/hooks/key",
        "https://mattermost.example/not-a-webhook",
        "https://mattermost.example/hooks/key\n",
    ],
)
def test_reject_unsafe_urls_without_echo(url):
    with pytest.raises(WebhookError) as error:
        validate_url("mattermost", url)
    assert url not in str(error.value)
    assert not error.value.retryable


def test_transport_pins_public_dns_uses_tls_text_json_and_never_redirects(monkeypatch):
    monkeypatch.setattr(
        "socket.getaddrinfo", lambda *args, **kwargs: [(2, 1, 6, "", ("8.8.8.8", 443))]
    )
    raw = MagicMock()
    connect = MagicMock(return_value=raw)
    monkeypatch.setattr("socket.create_connection", connect)
    context = MagicMock()
    monkeypatch.setattr("ssl.create_default_context", lambda: context)
    connection = MagicMock()
    monkeypatch.setattr("http.client.HTTPSConnection", lambda *args, **kwargs: connection)
    response = connection.getresponse.return_value
    response.status, response.read.return_value = 200, b"ok"
    send("mattermost", URL, "기획 승인 대기")
    connect.assert_called_once_with(("8.8.8.8", 443), timeout=5)
    context.wrap_socket.assert_called_once_with(raw, server_hostname="mattermost.example")
    assert json.loads(connection.request.call_args.kwargs["body"]) == {"text": "기획 승인 대기"}
    assert connection.request.call_args.args == ("POST", "/hooks/example-test-key")
    for status, retryable in [(302, False), (400, False), (429, True), (503, True)]:
        response.status = status
        with pytest.raises(WebhookError) as error:
            send("mattermost", URL, "test")
        assert error.value.retryable is retryable
        assert URL not in str(error.value)


def test_transport_blocks_private_dns_without_connecting(monkeypatch):
    monkeypatch.setattr(
        "socket.getaddrinfo", lambda *args, **kwargs: [(2, 1, 6, "", ("127.0.0.1", 443))]
    )
    connect = MagicMock()
    monkeypatch.setattr("socket.create_connection", connect)
    with pytest.raises(WebhookError):
        send("mattermost", URL, "test")
    connect.assert_not_called()
