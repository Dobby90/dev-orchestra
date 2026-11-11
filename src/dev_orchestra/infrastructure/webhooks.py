"""Small app adapter registry and a bounded, direct HTTPS webhook transport."""

import http.client
import ipaddress
import json
import re
import socket
import ssl
from urllib.parse import urlsplit


class WebhookError(Exception):
    """Only fixed, credential-free messages may cross the transport boundary."""

    def __init__(self, message, retryable=True):
        super().__init__(message)
        self.retryable = retryable


class Mattermost:
    label = "Mattermost"

    @staticmethod
    def validate_url(url):
        parts = urlsplit(url)
        if not re.fullmatch(r"/hooks/[A-Za-z0-9_-]+", parts.path):
            raise ValueError("invalid webhook path")

    @staticmethod
    def payload(text):
        return {"text": text}

    @staticmethod
    def accepted(status, body):
        return status == 200 and body.strip() == b"ok"


# Adding an app requires its URL validator, payload encoder and response validator.
APPS = {"mattermost": Mattermost}


def validate_url(app, url):
    try:
        parts = urlsplit(url)
        if (
            app not in APPS
            or len(url) > 2048
            or any(c.isspace() or ord(c) < 32 for c in url)
            or parts.scheme != "https"
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
            or "\\" in url
            or not 1 <= (parts.port or 443) <= 65535
        ):
            raise ValueError("invalid webhook URL")
        APPS[app].validate_url(url)
        try:
            address = ipaddress.ip_address(parts.hostname)
        except ValueError:
            if parts.hostname.lower() == "localhost" or parts.hostname.endswith(".localhost"):
                raise ValueError("local webhook URL") from None
        else:
            if not address.is_global:
                raise ValueError("non-public webhook URL")
    except (ValueError, KeyError):
        raise WebhookError(
            "앱에 맞는 공인 HTTPS 웹훅 URL을 입력해 주세요.", retryable=False
        ) from None


def send(app, url, text):
    validate_url(app, url)
    parts = urlsplit(url)
    adapter = APPS[app]
    connection = None
    raw = None
    try:
        addresses = socket.getaddrinfo(parts.hostname, parts.port or 443, type=socket.SOCK_STREAM)
        # Validate every answer and connect to the validated address, avoiding DNS rebinding.
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise WebhookError("공인 HTTPS 수신 주소만 사용할 수 있습니다.", retryable=False)
        context = ssl.create_default_context()
        connection = http.client.HTTPSConnection(
            parts.hostname, parts.port or 443, timeout=5, context=context
        )
        raw = socket.create_connection((addresses[0][4][0], parts.port or 443), timeout=5)
        connection.sock = context.wrap_socket(raw, server_hostname=parts.hostname)
        connection.request(
            "POST",
            parts.path,
            body=json.dumps(adapter.payload(text), ensure_ascii=False).encode(),
            headers={"Content-Type": "application/json", "User-Agent": "DevOrchestra"},
        )
        response = connection.getresponse()
        body = response.read(64)
        if not adapter.accepted(response.status, body):
            raise WebhookError(
                f"웹훅 수신 실패 (HTTP {response.status}).",
                retryable=response.status in {408, 429} or response.status >= 500,
            )
    except WebhookError:
        raise
    except (OSError, ValueError, http.client.HTTPException):
        # Never expose the URL, raw response, DNS exception or TLS exception.
        raise WebhookError("웹훅 연결에 실패했습니다. 주소와 네트워크를 확인해 주세요.") from None
    finally:
        if connection is not None:
            connection.close()
        if raw is not None:
            raw.close()
