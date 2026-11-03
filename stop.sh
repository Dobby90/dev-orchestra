#!/bin/bash
# 기동 시 사용한 설정으로 로컬 GUI 서비스를 종료합니다.
set -euo pipefail

caller_dir="$PWD"
repo_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
config_file=""
stop_args=(web stop)

usage() {
  cat <<'HELP'
사용법: ./stop.sh [옵션]
  --config 파일      기동 시 사용한 YAML 설정 파일
  --cancel-active    미완료 작업도 취소 (기본값: 실행 중 작업 유지)
  -h, --help         도움말

GUI·API와 해당 서비스가 시작한 Dispatcher를 종료합니다.
설정 파일을 지정해 기동했다면 종료에도 동일한 --config를 지정하세요.
HELP
}

fail() { printf '오류: %s\n' "$*" >&2; exit 1; }

while [ "$#" -gt 0 ]; do
  case "$1" in
    --config)
      [ "$#" -ge 2 ] && [ -n "$2" ] || fail "$1 뒤에 값을 지정하세요."
      case "$2" in
        /*) config_file="$2" ;;
        *) config_file="$caller_dir/$2" ;;
      esac
      shift 2 ;;
    --cancel-active) stop_args+=(--cancel-active); shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; fail "알 수 없는 옵션: $1" ;;
  esac
done

[ -z "$config_file" ] || [ -f "$config_file" ] || fail "설정 파일을 찾을 수 없습니다: $config_file"
cd "$repo_dir"
cli="$repo_dir/.venv/bin/devorchestra"
[ -x "$cli" ] || fail '먼저 저장소 루트에서 UV_CACHE_DIR="$PWD/.uv-cache" .tools/uv sync --frozen --extra web 으로 환경을 준비하세요.'

if [ -n "$config_file" ]; then
  exec "$cli" --config "$config_file" "${stop_args[@]}"
else
  exec "$cli" "${stop_args[@]}"
fi
