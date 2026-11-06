"""Native credential vaults and an ephemeral, memory-only Git credential broker."""

import json
import os
import shlex
import socket
import sys
import tempfile
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from dev_orchestra.application.errors import ApplicationError
from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure.process import execute, execution_directory


class CredentialVault:
    def backend(self):
        try:
            # Select native backends explicitly; never accept plaintext third-party backends.
            if sys.platform == "darwin":
                from keyring.backends.macOS import Keyring
            elif sys.platform == "win32":
                from keyring.backends.Windows import WinVaultKeyring as Keyring
            else:
                from keyring.backends.SecretService import Keyring
            return Keyring()
        except Exception:
            raise ApplicationError(
                "VAULT_UNAVAILABLE", "OS 자격 증명 저장소를 사용할 수 없습니다."
            ) from None

    def get(self, key):
        try:
            return self.backend().get_password("dev-orchestra.git", key)
        except Exception:
            raise ApplicationError(
                "VAULT_UNAVAILABLE", "OS 자격 증명 저장소 읽기에 실패했습니다."
            ) from None

    def set(self, key, token):
        try:
            self.backend().set_password("dev-orchestra.git", key, token)
        except Exception:
            raise ApplicationError(
                "VAULT_UNAVAILABLE", "OS 자격 증명 저장소 저장에 실패했습니다."
            ) from None

    def delete(self, key):
        try:
            backend = self.backend()
            if backend.get_password("dev-orchestra.git", key) is not None:
                backend.delete_password("dev-orchestra.git", key)
        except Exception:
            raise ApplicationError(
                "VAULT_UNAVAILABLE", "OS 자격 증명 저장소 삭제에 실패했습니다."
            ) from None


# This script receives only a private socket path. Secrets never enter argv or files.
HELPER = """import json, socket, sys
if sys.argv[-1] == "get":
    s = socket.socket(socket.AF_UNIX)
    s.connect(sys.argv[1])
    s.sendall(sys.stdin.read().encode())
    s.shutdown(socket.SHUT_WR)
    data = b""
    while chunk := s.recv(65536): data += chunk
    v = json.loads(data)
    print("username=" + v[0] + "\\npassword=" + v[1] + "\\n")
"""


@contextmanager
def credentials(account, token, url):
    if token is None:
        yield ["-c", "credential.username=" + account] if account else []
        return
    with tempfile.TemporaryDirectory(prefix="do-auth-", dir="/tmp") as directory:
        path = Path(directory)
        helper = path / "helper.py"
        helper.write_text(HELPER)
        # Keep Unix socket paths below the macOS limit, even under a long TMPDIR.
        address = str(path / "s")
        server = socket.socket(socket.AF_UNIX)
        server.bind(address)
        server.listen()
        server.settimeout(0.1)
        stopped = threading.Event()

        def serve():
            while not stopped.is_set():
                try:
                    conn, _ = server.accept()
                except TimeoutError:
                    continue
                with conn:
                    conn.settimeout(1)
                    try:
                        request = b""
                        while chunk := conn.recv(65536):
                            request += chunk
                            if len(request) > 65536:
                                break
                        fields = dict(
                            line.split("=", 1)
                            for line in request.decode().splitlines()
                            if "=" in line
                        )
                        parsed = urlsplit(url)
                        if (
                            fields.get("protocol") == "https"
                            and fields.get("host") == parsed.netloc
                            and fields.get("path", "") == parsed.path.lstrip("/")
                        ):
                            conn.sendall(json.dumps([account, token]).encode())
                        else:
                            conn.sendall(b'["", ""]')
                    except (OSError, ValueError):
                        pass

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            command = "!" + shlex.join([sys.executable, str(helper), address])
            yield [
                "-c",
                "credential.useHttpPath=true",
                "-c",
                "credential.helper=",
                "-c",
                "credential.helper=" + command,
            ]
        finally:
            stopped.set()
            thread.join()
            server.close()


def network_git(repo, args, account="", token=None, *, url=None):
    # Do not use the logged Git runner: transport stderr may echo remote secrets.
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("GIT_TRACE", "GIT_CONFIG_", "GIT_CURL_VERBOSE"))
    }
    env.update(GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="/usr/bin/false", SSH_ASKPASS="/usr/bin/false")
    directory = execution_directory()
    if directory is None:
        raise PipelineError("Git transport requires a durable process ownership scope")
    diagnostic = []
    diagnostic_size = 0
    output_noted = False

    def suppress(line):
        nonlocal diagnostic_size, output_noted
        if diagnostic_size < 65536:
            part = line[: 65536 - diagnostic_size]
            diagnostic.append(part)
            diagnostic_size += len(part)
        if not output_noted:
            output_noted = True
            return "[Git transport output omitted]\n"
        return ""

    with credentials(account, token, url) as options:
        try:
            code, _ = execute(
                [
                    "git",
                    "-C",
                    str(repo),
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-c",
                    "protocol.allow=never",
                    "-c",
                    "protocol.https.allow=always",
                    "-c",
                    "protocol.http.allow=always",
                    "-c",
                    "protocol.ssh.allow=always",
                    "-c",
                    "protocol.file.allow=always",
                    *options,
                    *args,
                ],
                repo,
                60,
                directory / "git-transport" / (uuid.uuid4().hex + ".log"),
                env=env,
                output_redactor=suppress,
            )
        except (OSError, PipelineError):
            return {"status": "network_error", "message": "네트워크 또는 Git 실행 오류입니다."}
    if code == 0:
        return {"status": "success", "message": "요청을 완료했습니다."}
    message = "".join(diagnostic).lower()
    if any(
        s in message
        for s in (
            "authentication failed",
            "could not read username",
            "could not read password",
            "invalid username or password",
            "http basic: access denied",
            "permission denied (publickey)",
            "401",
        )
    ):
        status, text = (
            "authentication_failed",
            "Git 인증에 실패했습니다. 계정과 인증 정보를 확인하세요.",
        )
    elif any(
        s in message
        for s in (
            "403",
            "access denied",
            "not found",
            "does not appear to be a git repository",
            "permission denied",
        )
    ):
        status, text = (
            "access_denied",
            "저장소 접근이 거부되었습니다. 주소와 접근 권한을 확인하세요.",
        )
    elif any(s in message for s in ("resolve host", "connect", "timed out", "network", "ssl")):
        status, text = "network_error", "네트워크 연결에 실패했습니다. 연결 상태를 확인하세요."
    elif "rejected" in message:
        status, text = (
            "rejected",
            "원격에서 Push를 거부했습니다. 브랜치 보호와 원격 이력을 확인하세요.",
        )
    else:
        status, text = (
            "failed",
            "Git 요청에 실패했습니다. 인증·원격 주소·권한을 확인한 뒤 재시도하세요.",
        )
    return {"status": status, "message": text}
