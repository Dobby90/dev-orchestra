#!/bin/bash
# 저장소의 준비된 Python 환경으로 로컬 GUI를 반복 기동합니다.
set -euo pipefail

caller_dir="$PWD"
repo_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
config_file=""
port="8000"
rebuild=0
allowed_roots=()

usage() {
  cat <<'HELP'
사용법: ./start.sh [옵션]
  --allow-root 경로  등록할 프로젝트의 상위 경로 (여러 번 지정 가능)
                    기본값: DevOrchestra 저장소의 상위 디렉터리
  --port 포트        기본값: 8000 (1024~65535)
  --config 파일      모든 명령에 동일하게 적용할 YAML 설정 파일
  --rebuild          GUI 의존성도 재설치 (서비스 종료 후 사용)
  -h, --help         도움말

이미 실행 중이면 기존 서비스 주소와 새 일회용 로그인 코드를 표시합니다.
서비스를 새로 기동할 때는 React GUI를 항상 빌드합니다.
SQLite는 처음에 자동 준비합니다. Colima나 별도 DB 서버는 사용하지 않습니다.
HELP
}

fail() { printf '오류: %s\n' "$*" >&2; exit 1; }

while [ "$#" -gt 0 ]; do
  case "$1" in
    --allow-root|--config|--port)
      [ "$#" -ge 2 ] && [ -n "$2" ] || fail "$1 뒤에 값을 지정하세요."
      case "$1" in
        --allow-root)
          case "$2" in
            /*) allowed_roots+=("$2") ;;
            *) allowed_roots+=("$caller_dir/$2") ;;
          esac ;;
        --config)
          case "$2" in
            /*) config_file="$2" ;;
            *) config_file="$caller_dir/$2" ;;
          esac ;;
        --port) port="$2" ;;
      esac
      shift 2 ;;
    --rebuild) rebuild=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; fail "알 수 없는 옵션: $1" ;;
  esac
done

[[ "$port" =~ ^[0-9]{1,5}$ ]] || fail "포트는 1024~65535 숫자여야 합니다."
port=$((10#$port))
[ "$port" -ge 1024 ] && [ "$port" -le 65535 ] || fail "포트는 1024~65535여야 합니다."
[ -z "$config_file" ] || [ -f "$config_file" ] || fail "설정 파일을 찾을 수 없습니다: $config_file"
if [ "${#allowed_roots[@]}" -eq 0 ]; then
  allowed_roots+=("$(dirname -- "$repo_dir")")
fi
for root in "${allowed_roots[@]}"; do
  [ -d "$root" ] || fail "프로젝트 허용 경로가 없습니다: $root"
done

cd "$repo_dir"
python="$repo_dir/.venv/bin/python"
cli="$repo_dir/.venv/bin/devorchestra"
[ -x "$python" ] && [ -x "$cli" ] || fail '먼저 저장소 루트에서 UV_CACHE_DIR="$PWD/.uv-cache" .tools/uv sync --frozen --extra web 으로 환경을 준비하세요.'
"$python" -c 'import fastapi, uvicorn, dev_orchestra' 2>/dev/null || fail '웹 의존성이 없습니다. .tools/uv sync --frozen --extra web 을 실행하세요.'

run_cli() {
  if [ -n "$config_file" ]; then
    "$cli" --config "$config_file" "$@"
  else
    "$cli" "$@"
  fi
}

show_access() {
  local actual_port="$1"
  printf '\n접속 주소: http://127.0.0.1:%s\n' "$actual_port"
  printf '일회용 로그인 코드 (5분 이내 사용):\n'
  run_cli web-session
  printf '\n종료 명령: '
  printf '%q ' "$repo_dir/stop.sh"
  if [ -n "$config_file" ]; then printf '%q ' --config "$config_file"; fi
  printf '\n일반 종료 후에도 실행 중 작업은 계속됩니다. 전체 취소는 --cancel-active를 명시하세요.\n'
}

status_json="$(run_cli web status)"
existing_port="$(printf '%s' "$status_json" | "$python" -c 'import json,sys; s=json.load(sys.stdin); print(s["port"] if s["alive"] else "")')"
if [ -n "$existing_port" ]; then
  [ "$rebuild" -eq 0 ] || fail '이미 실행 중입니다. stop.sh로 종료한 뒤 --rebuild를 사용하세요.'
  printf '이미 실행 중인 서비스를 사용합니다. 포트·허용 경로 변경은 종료 후 기동해야 반영됩니다.\n'
  printf 'GUI 소스 변경을 반영하려면 stop.sh로 종료한 뒤 start.sh로 다시 기동하세요.\n'
  show_access "$existing_port"
  exit 0
fi

command -v npm >/dev/null 2>&1 || fail 'GUI 빌드에 Node 22.12 이상과 npm이 필요합니다.'
command -v node >/dev/null 2>&1 || fail 'GUI 빌드에 Node 22.12 이상이 필요합니다.'
node -e 'const [m,n]=process.versions.node.split(".").map(Number); if(m<22 || (m===22 && n<12)) process.exit(1)' || fail 'Node 22.12 이상으로 GUI를 빌드하세요.'
if [ "$rebuild" -eq 1 ] || [ ! -d node_modules ]; then
  printf 'GUI 의존성을 설치합니다.\n'
  npm ci
fi
printf '최신 GUI 소스를 빌드합니다.\n'
"$python" scripts/build_web.py

# 현재 스키마면 실행 중 Worker의 유지보수 잠금을 건드리지 않습니다.
needs_upgrade="$("$python" - "$config_file" <<'PY'
import sys
from pathlib import Path
from dev_orchestra.application.service import RunService
from dev_orchestra.config import load_config
from dev_orchestra.infrastructure.migrations import CURRENT_SCHEMA, WORKSPACE_SCHEMA
store = RunService(load_config(Path(sys.argv[1]) if sys.argv[1] else None)).store
with store.connection() as db:
    print(int(db.execute("SELECT version FROM schema_version").fetchone()[0] not in {CURRENT_SCHEMA, WORKSPACE_SCHEMA}))
PY
)"
if [ "$needs_upgrade" = "1" ]; then
  printf 'SQLite DB를 준비합니다. 실행 중인 Worker가 있으면 업그레이드를 거부합니다.\n'
  run_cli database-upgrade
fi

start_args=(web start --detach --port "$port")
for root in "${allowed_roots[@]}"; do
  start_args+=(--allow-root "$root")
done
run_cli "${start_args[@]}" >/dev/null
printf 'DevOrchestra를 기동했습니다.\n'
show_access "$port"
