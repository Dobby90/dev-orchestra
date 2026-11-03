# DevOrchestra

Codex CLI를 활용해 기획, 개발, 테스트 작성·실행, 독립적인 코드 리뷰, 제한된 횟수의 자동 수정을 수행하는 로컬 Python CLI입니다. 사용자 승인은 검증된 커밋을 승인 상태로 기록하며, 대상 프로젝트의 원격 Push나 Merge를 수행하지 않습니다.

이 저장소는 모노레포로 관리합니다. 공통 Python 백엔드는 `src/dev_orchestra`에 있으며, React + TypeScript 앱은 `apps/web`에 있습니다. FastAPI 백엔드·독립 Worker·SSE와 연결되는 로컬 GUI와 CLI·GUI 통합 실행을 제공합니다.

GUI 개발 Phase 1에서는 공통 Application Service 계층을 분리했습니다. 기존 `RunService` 인터페이스는 유지하며, `queries`는 DB 조회, `recovery`는 명시적 상태 조정, `approvals`는 검증된 후보 승인, `execution`은 독립 Worker 시작, `events`는 저장 로그와 이벤트 조회를 담당합니다. `ProjectService`는 허용 경로·Git 저장소·테스트/리뷰 정책을 검증합니다. Phase 2에서는 프로젝트 영속 관리와 인증된 REST API를 추가했습니다. Phase 3에서는 DB 스키마 4, 독립 Dispatcher·Worker의 작업 소유권과 복구, 영속 이벤트·SSE 로그 재생을 추가했습니다. Phase 4에서는 로그인·프로젝트·작업 접수·실행 상세·로그·승인 화면을 추가했습니다. Phase 5에서는 JUnit 상세·리뷰 이슈 이력·Diff·수정 요청과 보류, 명령 접수 이력, 정적 GUI 패키징과 통합 시작·종료를 추가했습니다.

공통 서비스는 FastAPI나 Typer에 의존하지 않습니다. `queries.summary()`는 PRD·설정·서버 디렉터리를 제외한 타입이 있는 요약을 반환하고, 기존 CLI 출력에 필요한 내부 전체 조회는 별도로 유지합니다. `status()`의 기존 복구 동작은 유지되며, 읽기 전용 조회는 복구나 보고서 생성을 수행하지 않습니다.

## 설치

Git, Python 3.11 이상, Codex CLI, OS 샌드박스가 필요합니다. Codex CLI 0.166.0에서 명령 호환성과 OS 샌드박스를 검증했습니다. macOS에서는 Seatbelt를 사용하고, Linux에서는 `bwrap`이 필요합니다. 현재 MVP는 Windows를 지원하지 않습니다.

```sh
# uv가 설치된 경우
uv python install 3.12
uv sync --frozen
uv run devorchestra --help

# Python 3.11 이상이 이미 설치된 경우의 예시
python3.12 -m venv .venv
.venv/bin/python -m pip install -e .
```

현재 개발 작업 공간에는 독립 Python 3.12 런타임인 `.python`, uv 실행 파일이 있는 `.tools`, 가상환경인 `.venv`도 준비되어 있습니다. 이 디렉터리들은 Git에 포함되지 않습니다. 현재 작업 공간에서 잠금 파일에 맞춰 환경을 구성하고 테스트하려면 다음 명령을 사용합니다.

```sh
UV_CACHE_DIR="$PWD/.uv-cache" .tools/uv sync --frozen
.venv/bin/python -m pytest -q
```

Codex 인증은 `codex login`으로 별도 설정합니다. 인증과 사용 요금은 설치된 Codex의 설정 및 계정을 따릅니다. 일반 테스트에서는 모델을 호출하지 않습니다.

## 반복 기동 스크립트

처음 Python 웹 환경을 준비한 뒤에는 저장소 루트의 실행 가능한 `start.sh`로 기동합니다.

```sh
cd /Users/hycho-office/dev/workspace/tools/dev-orchestra
./start.sh --allow-root /Users/hycho-office/dev/workspace
```

SQLite를 처음 준비하고 GUI·API·Dispatcher를 시작한 뒤 접속 주소와 60초 유효 일회용 로그인 코드를 표시합니다. Colima나 별도 DB 서버는 필요하지 않습니다. 이미 실행 중이면 기존 프로세스와 포트를 재사용하고 새 로그인 코드만 발급합니다. 포트·허용 경로를 바꾸려면 기존 서비스를 종료하고 다시 시작하세요. 이 명령은 대기 중인 기존 작업이 있으면 Dispatcher가 실행할 수 있습니다.

- `--allow-root`의 기본값은 이 저장소의 상위 디렉터리이며 여러 번 지정할 수 있습니다.
- `--port 8001`로 포트를 변경합니다. 기본값은8000입니다.
- `--config /절대경로/config.yaml`로 CLI와 동일한 설정/DB를 사용합니다. 상대 경로는 명령을 실행한 디렉터리를 기준으로 해석합니다.
- 기존 GUI 빌드는 재사용합니다. GUI 변경을 반영하려면 서비스를 종료한 뒤 `./start.sh --rebuild --allow-root /프로젝트/상위경로`로 다시 빌드합니다.
- GUI 빌드가 없거나 `--rebuild`를 지정하면 Node22.12 이상·npm으로 `npm ci`와 GUI 빌드를 수행합니다. 빌드가 있으면 매번 npm 설치/빌드를 하지 않습니다.
- Python 환경은 `.venv`를 사용합니다. 없거나 웹 의존성이 빠져 있으면 `.tools/uv sync --frozen --extra web` 준비 명령을 안내하고 중단합니다.
- 구버전 DB 업그레이드는 기존 안전 검사·백업을 적용합니다. 현재 스키마는 불필요한 업그레이드를 하지 않으므로 실행 중 Worker를 유지한 웹 재기동도 가능합니다.

종료 명령은 스크립트 출력에도 표시됩니다. 기본 설정에서는 다음과 같습니다.

```sh
.venv/bin/devorchestra web stop
# 미완료 작업 취소까지 명시할 때
.venv/bin/devorchestra web stop --cancel-active
```

설정 파일을 지정해 기동했다면 종료에도 동일한 `--config`를 하위 명령 앞에 지정하세요. 서비스는 백그라운드로 실행하며 브라우저를 자동으로 열지 않습니다. Mac은 작업 중 실행 가능한 상태를 유지하세요.

## 설정

`config.example.yaml`을 복사해 설정 파일을 만들고, 테스트용 인터프리터와 명령을 대상 프로젝트에 맞게 수정합니다. `--config` 옵션은 하위 명령 앞에 지정합니다.

YAML의 상대 경로는 설정 파일 위치를 기준으로 해석합니다. 기본 상태 저장 위치는 저장소 외부의 `~/.devorchestra`이며, 대상 저장소 내부에 상태나 Worktree를 저장하도록 설정하면 실행을 거부합니다.

테스트 명령 ID는 호스트 설정의 허용 목록입니다. 모델이 임의의 셸 명령을 만들어 실행할 수 없으며, 명령은 셸 없이 인자 배열로 실행합니다. 테스트 0건, 전체 건너뛰기, 보고서 누락·형식 오류를 통과 처리하지 않도록 JUnit XML 보고서를 필수로 요구합니다.

Java/Maven, Gradle, JavaScript 테스트 도구도 JUnit XML을 생성하도록 설정하면 사용할 수 있습니다. DevOrchestra가 대상 프로젝트의 의존성을 설치하거나, 호스트 설정 없이 테스트 명령을 선택하지는 않습니다.

- `write_paths`: Test Agent가 생성한 패치를 적용할 수 있는 테스트 경로입니다.
- `output_paths`: Git이 추적하지 않는 빌드·보고서 출력 디렉터리입니다. 추적 중인 소스 파일을 포함한 디렉터리는 허용하지 않습니다.

테스트에 필요한 의존성은 오프라인에서 사용할 수 있도록 미리 준비해야 합니다. Maven·Gradle·npm은 프로젝트별 캐시 및 출력 경로 설정이 추가로 필요할 수 있습니다. 테스트 중 외부 네트워크 접근은 차단합니다.

## 명령어

```sh
devorchestra --config config.yaml doctor
devorchestra --config config.yaml run --repo ~/projects/example --spec PRD.md --base main --max-fix-attempts 3
devorchestra --config config.yaml run --repo ~/projects/example --spec PRD.md --detach
devorchestra status RUN_ID
devorchestra list
devorchestra logs RUN_ID --follow --role reviewer
devorchestra pause RUN_ID
devorchestra resume RUN_ID
devorchestra resume RUN_ID --answers answers.md
devorchestra resume RUN_ID --max-fix-attempts 5
devorchestra cancel RUN_ID
devorchestra report RUN_ID
devorchestra approve RUN_ID
devorchestra cleanup RUN_ID
```

기본 저장 위치를 변경했다면 이후 명령에도 동일한 `--config` 또는 `--database`를 지정해야 합니다. `codex-pipeline`은 `devorchestra`와 동일하게 동작하는 별칭입니다. 조회 명령은 JSON으로 결과를 출력합니다.

| 상황 | 종료 코드 |
|---|---|
| `run`·`resume`의 실행 실패 또는 사용자 개입 필요 | `2` |
| 명령·입력 오류 | `1` |
| 최종 승인 대기 도달 | `0` |

일반 실행 중 `Ctrl+C`를 누르면 안전한 일시중지를 요청합니다. `--detach`로 실행한 Worker는 CLI 종료 후에도 계속 동작합니다. 취소는 하위 프로세스가 종료된 뒤 확정됩니다.

각 실행 명령에는 별도 관리 프로세스가 있습니다. Worker가 강제 종료되면 관리 프로세스가 연결 종료를 감지해 Codex·테스트·실행 중인 Git 명령과 추적한 자식을 정리합니다. `status`는 소유권을 잃은 실행을 `failed`로 표시합니다. `cancel`과 `resume`은 정리를 확인한 뒤 진행하며, 관리 프로세스도 사라져 종료를 확인할 수 없으면 `needs_input`으로 남기고 수동 확인을 요구합니다. 디스크에 기록된 PID만으로 다른 프로세스를 종료하지 않습니다.

`cleanup`은 Worktree를 정리하되 브랜치, 커밋, 보고서, SQLite 이력을 보존합니다. 미커밋 변경이 있는 Worktree는 강제로 삭제하지 않습니다.

## 워크플로와 검증 근거

기본 실행 순서는 다음과 같습니다.

```text
queued → preparing → planning → developing → testing → reviewing
```

테스트가 실패하거나 통과를 차단하는 리뷰 문제가 발견되면 `fixing → testing → reviewing`을 반복합니다. 자동 수정은 기본 최대 3회입니다. 검증을 통과하면 보고서를 생성하고 `waiting_approval` 상태로 사용자 승인을 기다립니다.

- `needs_input`: PRD가 불명확하거나 수정 한도를 소진해 사용자 판단이 필요한 상태입니다.
- `failed`: 프로세스 실행 등에서 오류가 발생한 상태입니다.
- `resume`: 체크포인트를 검증하고 완료된 단계는 보존한 채 재개합니다. 수정 횟수는 초기화하지 않습니다.
- `approve`: 검증한 HEAD가 유지되고 미커밋 변경이 없으며 검증 자료가 변조되지 않았을 때 승인합니다.

모든 에이전트는 새로 생성한 읽기 전용 Codex 세션에서 실행하고 구조화된 결과를 반환합니다. 코드 변경은 호스트가 unified diff 패치를 검사·적용하는 방식으로 수행합니다. Developer와 Fix Agent는 기존 테스트를 수정할 수 없습니다.

파일 삭제·이름 변경, 바이너리 패치, 심볼릭 링크, 권한 변경은 사용자의 별도 처리가 필요합니다. Test Agent 패치는 설정된 테스트 경로에만 적용합니다. 자동 수정 반복 중에는 Planning 단계에서 확정한 인수 조건을 변경하지 않습니다. 모델이 생성한 테스트 대응 관계는 추적을 위한 자료이며, 요구사항 충족을 수학적으로 증명하지는 않습니다.

산출물은 `runs/RUN_ID/iterations/ITERATION/ROLE/attempt-NNN/` 아래에 회차·역할·시도별로 저장합니다. 각 시도에는 스키마, 응답, 로그, 결과, 프로세스 기록이 남습니다. 실행 디렉터리 최상위에는 최신 요약, PRD·설정 스냅샷, `final-report.md`를 저장합니다.

실행 디렉터리의 `worker.json`과 `launch.json`에는 Worker 식별 정보를, `processes/`에는 명령별 관리 프로세스·생성 시각·종료 확인 결과를 저장합니다. `git/`에는 실행 중 Git 명령의 마스킹된 로그를 저장합니다. OS 프로세스 조회가 차단된 환경에서는 종료 소유권을 확인할 수 없어 안전하게 실행을 거부할 수 있습니다.

상태와 이벤트 이력은 SQLite에 저장하며 WAL, 외래 키, 짧은 트랜잭션을 사용합니다. OS 잠금으로 데이터베이스와 저장소별 실행을 직렬화합니다. 최종 결과와 승인은 Git 커밋 SHA에 연결됩니다. 승인한 결과가 변경되면 사용자 개입이 필요한 상태로 표시하고, 과거 승인 이력은 보존합니다.

## 안전성 및 현재 제약

- **기존 작업 환경 보존**: 원래 작업 디렉터리의 수정 내용과 현재 브랜치를 유지합니다. Worktree에는 기준 커밋의 코드만 가져오며, 미커밋 변경은 복사하지 않습니다.
- **Git 작업 제한**: Feature 브랜치와 Worktree는 Git 메타데이터를 공유합니다. 에이전트는 이를 수정할 수 없습니다. 호스트의 Git 명령은 hooks·서명·fsmonitor를 비활성화하고 대상 프로젝트를 Push하거나 Merge하지 않습니다.
- **테스트 격리**: 소스는 읽기 전용으로 두고 지정한 출력 디렉터리만 쓰기를 허용합니다. 임시 HOME과 선별한 환경변수를 사용하며, 네트워크는 차단합니다. macOS에서는 알려진 인증 정보 경로와 프로젝트의 `.env`·키 파일 읽기를 차단합니다. 다만 완전한 악성 코드 격리를 보장하지는 않으며, 다른 위치의 알려지지 않은 비밀 파일은 읽힐 수 있습니다. Linux의 bwrap 백엔드는 별도 배포 검증이 필요하고 읽기 허용 범위도 더 넓습니다.
- **Codex 읽기 범위**: 읽기 전용 샌드박스는 쓰기를 제한하지만 관련 없는 모든 호스트 파일의 읽기까지 차단하지는 않습니다. 신뢰할 수 없는 저장소는 전용 계정이나 컨테이너에서 실행해야 합니다.
- **프롬프트 인젝션과 테스트 무력화**: PRD나 저장소 파일은 호스트의 테스트 명령을 선택하거나 결과를 승인할 수 없습니다. 프롬프트 인젝션과 테스트의 의미적인 약화를 완전히 자동 탐지할 수는 없습니다.
- **수동 확인이 필요한 복구**: 커밋 직후 체크포인트 기록 전 종료, 외부에서 변경된 HEAD, 미커밋 변경, 관리 프로세스까지 사라진 실행은 직접 확인해야 합니다. 기존 버전의 미완료 프로세스 기록도 소유권 확인 없이 자동 종료하지 않습니다. 데이터는 보존하며 자동 reset·clean으로 복구 성공을 가장하지 않습니다.
- **승인의 유효 범위**: 승인은 특정 SHA에 대한 이력입니다. 파일시스템 감시기는 없으며, `status`에서 현재 승인 유효성을 확인합니다.
- **로그와 민감정보**: 로그는 스트리밍하며 저장 전에 비밀값을 마스킹합니다. 프로세스별 출력은 최대 8 MiB입니다. 모델의 구조화 응답과 PRD 스냅샷에는 민감한 프로젝트 내용이 포함될 수 있으므로 상태 저장소를 비공개로 관리해야 합니다. 모든 비밀값 형식을 자동 식별할 수는 없습니다.
- **동시 실행**: SQLite 데이터베이스별로 Worker 하나만 실행합니다. 서로 다른 데이터베이스가 같은 Git common directory를 대상으로 작업해도 저장소 잠금으로 직렬화합니다.
- **구현 범위**: 원격 실행, 대상 프로젝트 의존성 자동 설치, 자동 Push·Merge, 분산 큐는 포함하지 않습니다.

## 개발 및 검증

```sh
uv run pytest -q
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts

# 모델 사용량이 발생하므로 실행 전 사용자 승인이 필요합니다.
uv run python scripts/e2e.py

# 모델 호출 없이 실제 OS 샌드박스를 검증합니다.
DEVORCHESTRA_TEST_SANDBOX=1 uv run pytest tests/test_security.py -q
```

선택 실행하는 E2E는 임시 Git 저장소를 만들고, 실제 Codex 에이전트와 OS 샌드박스의 pytest를 실행합니다. 성공한 결과를 승인하고 원래 작업 디렉터리가 보존되는지 확인합니다. 실행 후 확인할 수 있도록 임시 산출물 위치를 출력하며, 실제 모델 실행의 실패를 성공으로 처리하지 않습니다.

상세 검증 결과는 [검증 기록](docs/VALIDATION.md)에서 확인할 수 있습니다.

## 품질 검증 후 보완 사항

GUI 착수 전 감사에서 발견한 결함의 수정·검증 결과는 [수정 및 검증 기록](docs/cli-mvp-remediation-results.md)을 참고하세요. 과거 준비도 보고서는 수정 전 커밋에 대한 감사 기록으로 보존합니다.

- 최종 승인 전에 테스트·리뷰·현재 후보 SHA와 저장된 체크포인트를 함께 검증합니다.
- 기존 테스트 파일은 Test Agent도 수정할 수 없습니다. 필요한 테스트는 새 파일로 추가합니다.
- 실행 가능한 Git clean/smudge/process 필터가 설정된 저장소는 안전 정책이 마련될 때까지 실행을 거부합니다. Git LFS 등 필터를 사용하는 프로젝트도 해당할 수 있습니다.
- 사전 점검과 잠금 대기 중에도 취소를 처리하고, 프로세스 그룹에 남은 자식을 종료합니다.
- Worker 강제 종료 후에도 별도 관리 프로세스가 자식을 정리합니다. 중복 Worker는 기존 소유권을 덮어쓰지 않으며, 종료 확인 실패를 취소 성공으로 표시하지 않습니다.
- CLI 기본 DB는 스키마 2를 유지하며, 웹 백엔드·큐에는 명시적으로 스키마 5로 업그레이드합니다. 기존 버전 1 데이터도 CLI의 기존 1→2 갱신 후 보존합니다. 알 수 없는 버전은 거부합니다.
- 승인·무효화 후 보고서를 갱신하며, 제어·단계 이벤트와 파일별 로그 커서를 제공합니다. FastAPI·SSE·React는 공통 서비스를 사용하는 별도 어댑터로 제공합니다.

## 로컬 API와 독립 Worker

웹 의존성은 선택 설치합니다. 아래 명령은 기존 개발 가상환경을 사용하는 예시이며 `--config`는 각 명령 앞에 지정합니다.

```sh
UV_CACHE_DIR="$PWD/.uv-cache" .tools/uv sync --frozen --extra web
.venv/bin/devorchestra --config config.yaml database-upgrade
.venv/bin/devorchestra --config config.yaml api-start --allow-root ~/projects
```

`database-upgrade`는 실행 중인 Worker·미확인 프로세스·실행 잠금이 있으면 중단합니다. schema 2·3·4의 일관된 SQLite 백업과 증거 참조 manifest를 DB 옆에 저장한 뒤 트랜잭션으로 schema 5까지 업그레이드합니다. API·웹 서비스·Dispatcher도 먼저 중지해야 합니다. 구버전 CLI/Worker와 혼용하지 마세요. 복원은 모든 writer를 종료한 상태에서 백업으로 수행합니다. 기존 데이터베이스는 API 시작만으로 자동 업그레이드하지 않습니다.

API는 `127.0.0.1:8000`에만 바인딩하며 세션·정확한 Origin/Host·쓰기 CSRF 검사를 적용합니다. `--allow-root` 아래의 Git 저장소만 프로젝트로 등록할 수 있습니다. 원본 저장소·브랜치를 삭제하지 않으며 프로젝트 DELETE는 등록 해제(archive)입니다.

```sh
# 같은 DB에 60초 유효·일회용 로그인 코드를 발급합니다.
.venv/bin/devorchestra --config config.yaml web-session
```

로그인 코드는 로컬 클라이언트가 허용된 Origin에서 `POST /api/session`으로 전달합니다. 응답의 HttpOnly 세션 쿠키와 CSRF 토큰으로 후속 API를 사용합니다. 인증값을 URL query나 로그에 넣지 마세요. `/api/openapi.json`과 `/api/docs`는 인증된 OpenAPI JSON을 제공하며, 외부 CDN을 사용하는 Swagger UI는 제공하지 않습니다. 브라우저 로그인 UI는 `apps/web`에서 제공합니다.

현재 API는 프로젝트 등록/조회/정책 수정/해제, PRD 업로드, 실행 preview, 멱등 실행 접수, 실행 목록/상세/단계/저장 로그/이벤트/기존 보고서 조회, 취소·일시중지·검증 후 승인·명시적 상태 조정을 제공합니다. 생성 응답은 DB에 접수된 `queued` 상태입니다. Dispatcher가 없으면 `queued_reason=dispatcher_offline`, 실행 중이면 `waiting_capacity`로 반환합니다. API 프로세스는 모델을 실행하지 않으며, 별도로 시작한 Dispatcher가 큐를 소비합니다. 재개·수정 요청·승인 보류·Worktree 정리·비차단 이슈 분류와 테스트·리뷰·Diff·artifact 조회 API를 제공합니다. 기존 CLI 직접 실행은 Dispatcher 없이도 유지됩니다.

```sh
# 동일한 config/DB를 사용하는 독립 Dispatcher를 시작합니다.
.venv/bin/devorchestra --config config.yaml worker start --detach
.venv/bin/devorchestra --config config.yaml worker status
.venv/bin/devorchestra --config config.yaml worker stop
```

`worker stop`은 Dispatcher의 새 작업 접수를 중지하며 실행 중인 Run Worker는 계속 동작합니다. 개별 작업은 기존 `pause`/`cancel` 경로로 제어합니다. API·SSE·Dispatcher 종료를 Run 취소로 해석하지 않습니다. claim 토큰은 Worker의 비공개 stdin으로 전달하고, PID와 생성 시각·실행 잠금을 함께 확인합니다. heartbeat 지연만으로 살아 있는 Worker를 재실행하지 않습니다. 실행 시작 전 사라진 job은 최대 3회 시작을 시도하고, 실행 중 Worker 상실은 자동 재실행하지 않으며 `failed` 또는 `needs_input`으로 보존해 명시적 재개를 요구합니다.

인증된 `GET /api/runs/{id}/events`는 SSE를 제공합니다. `Last-Event-ID`가 `after`보다 우선하며 서버 재시작 후에도 같은 영속 event ID로 재생합니다. `/event-history`는 같은 이벤트 envelope, `/logs`는 schema 2의 이벤트 커서 페이지를 반환합니다. `/logs?after=...&limit=...&role=developer`에서 필터 결과가 비어도 `next_cursor`로 계속 조회합니다. 기존 CLI의 파일별 로그 커서는 그대로 유지합니다.

로그는 UTF-8 줄 전체를 마스킹한 비공개 `log-views` 파일에 보존한 뒤 최대 4 KiB 구간과 해시를 DB 이벤트에 연결합니다. API는 원본 파일 경로나 인증 capability를 노출하지 않습니다. 일반 소스의 로그 레벨은 `unknown`으로 유지하고 구조가 확인된 workflow 로그만 레벨을 추출합니다. 8 MiB를 넘는 단일 줄은 안전 한도로 인덱싱 오류를 기록합니다. 소스 원본은 CLI 진단용으로 보존하므로 상태 디렉터리를 비공개로 관리해야 합니다. 모든 비밀값 형식을 자동으로 식별하지는 못합니다.


## React GUI와 통합 실행 — Phase 5

Node 22.12 이상에서 저장소 루트의 npm workspace를 사용합니다. Python CLI는 Node나 웹 의존성 설치 없이 계속 사용할 수 있습니다.

```sh
npm ci
# 앞 절의 API·Dispatcher를 같은 config로 시작한 뒤 실행합니다.
npm run dev:web
```

`http://127.0.0.1:5173`에 접속하여 `web-session`으로 발급한 코드를 입력합니다. 프로젝트 등록 → PRD 작성·업로드 → 실행 전 확인 → 작업 시작 → 로그·타임라인·보고서 → SHA 확인 후 승인 흐름을 제공합니다. 상태 제어 결과를 낙관적으로 완료 표시하지 않으며 SSE 단절 시 주기적 조회로 복구합니다.

개발 서버 없이 한 포트에서 실행하려면 정적 파일을 빌드한 뒤 통합 명령을 사용합니다. Node는 빌드할 때만 필요하며, GUI 산출물을 포함한 wheel을 설치한 환경에서는 Python만으로 실행합니다.

```sh
npm ci
.venv/bin/python scripts/build_web.py
# 실행 중인 API·웹 서비스·Dispatcher·Run Worker를 먼저 종료합니다.
.venv/bin/devorchestra --config config.yaml database-upgrade
.venv/bin/devorchestra --config config.yaml web start --detach --allow-root ~/projects --port 8000
.venv/bin/devorchestra --config config.yaml web-session
.venv/bin/devorchestra --config config.yaml web status
.venv/bin/devorchestra --config config.yaml web stop
```

`http://127.0.0.1:8000`에서 로그인합니다. `web start`는 정적 파일의 manifest와 체크섬, 포트·실행 소유권을 확인한 후 API·Dispatcher를 시작합니다. 기존 Dispatcher는 재사용하고 해당 프로세스를 종료하지 않습니다. 일반 `web stop`은 자신이 시작한 API·Dispatcher만 종료하며 실행 중 Run Worker를 유지합니다. DB의 미완료 작업까지 취소하려면 `web stop --cancel-active`를 명시합니다. GUI 파일이 없는 CLI 패키지는 기존 CLI를 계속 제공하며 `web start`에서 빌드 필요성을 안내합니다.

테스트 탭은 실제 명령별 시간·종료 코드·JUnit 실패 상세와 과거 회차를 표시합니다. 근거가 없으면 미상/사용 불가로 표시합니다. 리뷰 V2는 이전 이슈 ID마다 현재 후보 SHA와 같은 revision의 테스트를 근거로 재검증해야 해결로 인정하며, 리뷰에서 빠진 이슈를 자동 해결하지 않습니다. 차단 심각도의 이슈는 사용자 분류로 우회할 수 없습니다. Diff·JUnit·보고서는 체크섬을 검증하는 별도 산출물로 보존하여 Worktree 정리 후에도 조회합니다.

수정 요청은 승인 대기에서 새 revision·지시문·Fix/Test/Review 작업을 함께 저장합니다. 변경 없는 Fix에도 새 검증이 필요하며 수정 한도는 초기화하지 않습니다. 명령은 접수/적용/사용자 확인 필요 상태로 추적하고 동일 키·본문 재요청은 같은 operation을 반환합니다. 승인 이력은 특정 SHA에만 적용되고 Push·Merge를 수행하지 않습니다. schema 5에서 CLI도 같은 결과·검증 서비스를 사용합니다. 미등록 CLI 작업은 웹에서 조회 전용입니다.

```sh
devorchestra --config config.yaml request-changes RUN_ID --instructions changes.md --detach
devorchestra --config config.yaml defer RUN_ID --reason "추가 확인 후 승인"
# GUI 산출물을 포함한 배포 파일 생성
UV_CACHE_DIR="$PWD/.uv-cache" .tools/uv build --offline
```

상세 설치·검증 명령과 남은 검증 범위는 [웹 앱 안내](apps/web/README.md)에 있습니다. Phase 6 종합 검증을 수행했으며 현재 검증된 사용 범위는 macOS의 단일 사용자·신뢰하는 로컬 프로젝트입니다. Linux·다른 브라우저·장시간 부하 검증 범위는 아래에 구분합니다.


## GUI MVP 최종 검증 — Phase 6

현재 출고 판정은 **READY**입니다. macOS·단일 사용자·신뢰하는 로컬 프로젝트·Chromium 범위에서 Python164개·React23개·실제 Codex의 Fix/V2 재검증·패키지 검사와 최종 개발 서버/정적 GUI E2E 각3개가 통과했습니다. 이전 브라우저 시간 초과 기록은 보존하며, 사용자가 확인한 절전·잠금·세션 중단 이후 연속 관측한 재검증에서 제품 코드·단언·시간 제한 변경 없이 통과했습니다. 강제 절전/OS 중단 뒤의 모든 복구를 검증했다는 뜻은 아닙니다.

공통 서비스·API·SQLite 큐·독립 Worker·SSE·React·CLI 통합에 대한 회귀와 브라우저 검사를 제공합니다. 웹 관리 프로세스가 강제 종료되면 비공개 파이프의 연결 종료로 자신이 시작한 API·Dispatcher가 종료됩니다. 실행 중 Run Worker와 재사용한 외부 Dispatcher는 유지합니다. 시작 중 종료·중복 시작·포트 충돌도 기존 프로세스를 임의로 종료하지 않습니다. 웹 로그와 비공개 설정 파일은 0600으로 저장합니다.

Git 자동 커밋은 출력 경로를 제외한 변경 목록을 먼저 구한 뒤 리터럴 NUL 경로로 전달합니다. `.gitignore`에 등록된 `build/`·`.pytest_cache/`가 있어도 출력 파일을 강제 추가하지 않습니다. 모델이 변경한 모든 파일이 후보 커밋에 포함되고 차이가 없는지도 확인하며, 무시된 파일로 검증을 통과시키지 않습니다. Git은 `add --pathspec-from-file`을 지원하는 버전을 사용하세요.

재현 가능한 검증 명령입니다. 일반 검사는 유료 모델을 호출하지 않습니다.

```sh
DEVORCHESTRA_TEST_SANDBOX=1 .venv/bin/python -m pytest -v
.venv/bin/ruff check src tests scripts apps/web/e2e/backend.py
npm run test:web
npm run typecheck:web
.venv/bin/python scripts/benchmark_logs.py
.venv/bin/python scripts/build_web.py
PLAYWRIGHT_BROWSERS_PATH=/private/tmp/devorchestra-pw-browsers npm run test:e2e --workspace apps/web
E2E_BUNDLED=1 PLAYWRIGHT_BROWSERS_PATH=/private/tmp/devorchestra-pw-browsers npm run test:e2e --workspace apps/web
```

wheel을 설치한 독립 환경 두 개를 준비했다면 다음 검사기로 Node 없는 실행과 웹 의존성 없는 CLI를 확인할 수 있습니다. 웹 환경에는 `web` extra를 설치하고, CLI 환경에는 FastAPI·Uvicorn을 설치하지 않습니다.

```sh
.venv/bin/python scripts/verify_package.py --web-python /절대경로/web-env/bin/python --core-python /절대경로/core-env/bin/python
```

실제 모델 검사는 인증과 사용량을 확인한 뒤 별도로 실행합니다. 아래 명령은 임시 저장소에서 실제 HTTP·Dispatcher·Worker·Codex를 사용합니다. 검사기가 Developer 응답의 덧셈 한 줄을 뺄셈으로 바꾸는 결함 주입을 명시적으로 수행하고 원래 응답을 보존합니다. Test 실패→실제 Fix→Test/Reviewer V2 재검증→CLI 승인과 원본 저장소 보존을 확인하며 한 실행당 모델 호출은 최대 7회, 역할별 제한 시간은 180초입니다. 일반 테스트·CI에서 자동 실행하지 마세요.

```sh
.venv/bin/python scripts/e2e_gui.py --allow-model-calls --model 사용할_모델_ID
```

배포 의존성 감사는 0건이고, 개발 전용 타입 생성기의 Moderate 3건과 TestClient의 httpx 전환 경고는 남아 있습니다. 장시간 메모리/부하·Linux bwrap·Firefox/WebKit·OS 재부팅/디스크 고장 검증은 완료로 간주하지 않습니다. 악성 저장소에 대한 완전한 호스트 읽기 격리, 원격/다중 사용자 운용은 지원 범위에 포함하지 않습니다. 상세 결과는 로컬 [Phase 6 검증 기록](docs/gui-phase6-results.md)과 [GUI MVP 최종 판정](docs/gui-mvp-readiness-report.md)에 보존합니다.

장시간 무인 검사는 컴퓨터가 실행 가능한 상태를 유지해야 합니다. macOS에서 검사를 실행하는 동안만 유휴 절전을 방지하려면 `/usr/bin/caffeinate -i env E2E_BUNDLED=1 PLAYWRIGHT_BROWSERS_PATH=/private/tmp/devorchestra-pw-browsers npm run test:e2e --workspace apps/web`을 사용할 수 있습니다. 영구 전원 설정은 변경하지 않으며 명시적 절전·잠금·세션 중단 전체를 방지하는 기능은 아닙니다.
