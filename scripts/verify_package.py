"""Verify installed GUI/core wheels with isolated state and no Node or model calls."""

import argparse
import http.cookiejar
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


def probe(kind):
    import dev_orchestra

    assert Path(dev_orchestra.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
    assert shutil.which("node") is None
    root = Path(tempfile.mkdtemp(prefix="devorchestra-installed-gate-")).resolve()
    config = root / "config.yaml"
    config.write_text(
        json.dumps(
            {
                "storage": {
                    "database": str(root / "state.db"),
                    "runs_directory": str(root / "runs"),
                    "worktrees_directory": str(root / "worktrees"),
                }
            }
        )
    )
    binary = Path(sys.executable).parent / "devorchestra"

    def cli(*args, expected=0):
        result = subprocess.run(
            [str(binary), "--config", str(config), *args],
            text=True,
            capture_output=True,
            timeout=30,
            cwd=root,
        )
        assert result.returncode == expected, (
            args,
            result.returncode,
            result.stdout,
            result.stderr,
        )
        return result.stdout + result.stderr

    cli("--help")
    assert json.loads(cli("list")) == []
    cli("database-upgrade")
    assert not json.loads(cli("web", "status"))["alive"]
    if kind == "core":
        assert importlib.util.find_spec("fastapi") is None
        assert importlib.util.find_spec("uvicorn") is None
        assert "install the web extra" in cli("web", "start", "--detach", expected=1)
        print("PASS: CLI 전용 설치, FastAPI/Uvicorn/Node 없음, 격리 DB·조회·마이그레이션·설치 안내")
        return
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    try:
        cli("web", "start", "--detach", "--allow-root", str(root), "--port", str(port))
        with urllib.request.urlopen(url + "/", timeout=10) as response:
            html = response.read().decode()
            assert '<div id="root"></div>' in html
            assert "script-src 'self'" in response.headers["Content-Security-Policy"]
        import hashlib

        assets = Path(dev_orchestra.__file__).parent / "_web"
        manifest = json.loads((assets / "manifest.json").read_text())
        for path, sha in manifest.items():
            if path == "index.html":
                continue
            with urllib.request.urlopen(url + "/" + path, timeout=10) as response:
                assert hashlib.sha256(response.read()).hexdigest() == sha
        cookiejar = http.cookiejar.CookieJar()
        client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookiejar))

        def request(path, data=None, headers=None):
            return client.open(
                urllib.request.Request(
                    url + path,
                    data=None if data is None else json.dumps(data).encode(),
                    headers={"Origin": url, "Content-Type": "application/json", **(headers or {})},
                ),
                timeout=10,
            )

        try:
            request("/api/runs")
            raise AssertionError("unauthenticated API exposed")
        except urllib.error.HTTPError as error:
            assert error.code == 401
        with request("/api/session", {"bootstrap_code": cli("web-session").strip()}) as response:
            assert response.status == 201
            json.load(response)
        with request("/api/runs") as response:
            assert json.load(response)["items"] == []
        assert root.joinpath("state.web.log").stat().st_mode & 0o777 == 0o600
        print(
            f"PASS: Node 없는 설치 GUI, HTTP·정적 파일 {len(manifest)}개 체크섬·CSP·인증·CLI 조회"
        )
    finally:
        cli("web", "stop")
        deadline = time.monotonic() + 15
        while json.loads(cli("web", "status"))["alive"] and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not json.loads(cli("web", "status"))["alive"]
    print("PASS: 소유 웹 서비스 종료; 격리 상태:", root)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--web-python", type=Path)
    parser.add_argument("--core-python", type=Path)
    parser.add_argument("--probe", choices=["web", "core"], help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.probe:
        probe(args.probe)
        return
    if not args.web_python or not args.core_python:
        parser.error("--web-python 및 --core-python에 wheel을 설치한 독립 가상환경을 지정하세요")
    env = dict(os.environ, PATH="/usr/bin:/bin")
    env.pop("PYTHONPATH", None)
    for kind, python in [("core", args.core_python), ("web", args.web_python)]:
        subprocess.run(
            [
                str(python.expanduser().absolute()),
                "-I",
                str(Path(__file__).resolve()),
                "--probe",
                kind,
            ],
            env=env,
            check=True,
            timeout=90,
            cwd=tempfile.gettempdir(),
        )


if __name__ == "__main__":
    main()
