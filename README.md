# DevOrchestra

Codex CLI를 활용해 기획, 개발, 테스트 작성·실행, 독립적인 코드 리뷰, 제한된 횟수의 자동 수정을 수행하는 로컬 개발 도구입니다. Python CLI와 React GUI가 같은 서비스·SQLite 상태·검증 근거를 사용합니다. 최종 승인은 검증된 Git 커밋 SHA에 대한 이력으로 기록하며, 대상 프로젝트를 자동으로 Push하거나 Merge하지 않습니다.

주요 기능은 다음과 같습니다.

- 별도 Git Worktree에서 실행하고 체크포인트를 기준으로 일시중지·재개·취소합니다.
- 호스트가 허용한 테스트 명령과 JUnit XML, 독립 리뷰를 함께 검증합니다.
- GUI에서 프로젝트 등록, PRD 작성·업로드, 실행 전 확인, 로그·타임라인·테스트·리뷰·Diff·보고서 조회와 승인을 제공합니다.
- 독립 Dispatcher·Run Worker와 영속 이벤트·SSE로 브라우저 연결과 작업 실행을 분리합니다.
- 승인 대기 중 수정 요청·승인 보류, 검증 산출물 보존과 Worktree 정리를 지원합니다.

## 요구 환경

| 구분 | 요구 사항 |
|---|---|
| CLI | Python 3.11 이상, Git, 인증된 Codex CLI |
| 테스트 격리 | macOS Seatbelt 또는 Linux `bwrap` |
| 로컬 GUI/API | Python의 `web` extra |
| GUI 소스 개발·빌드 | Node 22.12 이상, npm |
| 브라우저 검사 | Playwright Chromium |

현재 검증된 사용 범위는 macOS의 단일 사용자·신뢰하는 로컬 프로젝트·Chromium입니다. Linux `bwrap`, Firefox/WebKit, 장시간 부하와 OS 재부팅·디스크 장애 복구는 별도 검증이 필요합니다. Windows, 원격 실행, 다중 사용자 운영, 분산 큐는 지원하지 않습니다. Git은 `add --pathspec-from-file`을 지원해야 합니다.

## 설치

저장소 루트에서 실행합니다. `uv`를 사용하면 잠금 파일에 맞춰 개발 환경을 준비할 수 있습니다.

```sh
uv python install 3.12
uv sync --frozen
.venv/bin/devorchestra --help

# GUI/API도 사용할 경우
uv sync --frozen --extra web
```

이미 Python이 설치되어 있다면 CLI를 다음과 같이 설치할 수도 있습니다. Python 명령 이름은 환경에 맞게 선택하세요.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
# GUI/API도 사용할 경우
.venv/bin/python -m pip install -e '.[web]'
```

Codex 인증은 `codex login`으로 별도 설정합니다. 실제 작업 실행에는 모델 사용량이 발생하며 일반 단위·통합 검사는 실제 모델을 호출하지 않습니다. 아래 명령 예시의 `devorchestra`는 가상환경을 활성화하거나 `.venv/bin/devorchestra`로 실행하세요. `codex-pipeline`도 같은 CLI의 별칭입니다.

## GUI 빠른 시작과 종료

Python 웹 환경을 준비한 뒤 저장소 루트에서 실행합니다. 예시의 허용 경로는 실제 프로젝트의 상위 디렉터리로 바꾸세요.

```sh
uv sync --frozen --extra web
./start.sh --allow-root ~/projects
```

`start.sh`는 `.venv`를 사용해 GUI를 빌드하고 SQLite를 준비한 뒤 GUI·API·Dispatcher를 백그라운드로 시작합니다. 별도 DB 서버는 필요하지 않습니다. 출력된 `http://127.0.0.1:8000` 주소로 접속하고 일회용 코드를 입력하세요. 브라우저는 자동으로 열지 않습니다. Dispatcher를 시작하면 기존 대기 작업도 실행할 수 있습니다.

| 옵션 | 동작 |
|---|---|
| `--allow-root 경로` | 프로젝트 등록을 허용할 상위 경로입니다. 여러 번 지정할 수 있으며 기본값은 이 저장소의 상위 디렉터리입니다. |
| `--port 8001` | 1024~65535 범위의 포트입니다. 기본값은 8000입니다. |
| `--config config.yaml` | CLI와 공유할 설정 파일입니다. 상대 경로는 명령을 실행한 디렉터리를 기준으로 해석합니다. |
| `--rebuild` | GUI 의존성도 `npm ci`로 재설치합니다. 기존 서비스를 종료한 뒤 사용합니다. |

새로 기동할 때는 항상 최신 React 소스를 빌드하므로 Node와 npm이 필요합니다. `node_modules`가 없으면 `npm ci`를 먼저 실행합니다. 이미 실행 중이면 기존 프로세스·포트를 재사용하고 새 로그인 코드만 발급합니다. GUI 소스·포트·허용 경로 변경을 반영하려면 종료 후 다시 시작하세요.

```sh
./stop.sh

# 미완료 작업도 취소할 경우
./stop.sh --cancel-active

# 별도 설정 파일로 기동한 경우 종료에도 같은 파일을 지정합니다.
./stop.sh --config config.yaml
```

일반 종료는 해당 서비스가 시작한 API·Dispatcher를 종료하며 실행 중 Run Worker는 유지합니다. 재사용한 외부 Dispatcher도 유지합니다. 중복 시작과 포트 충돌은 기존 프로세스를 임의로 종료하지 않습니다. 구버전 DB 업그레이드가 필요한 경우에는 아래 마이그레이션 조건을 따르며, 현재 스키마인 DB는 불필요하게 업그레이드하지 않습니다.

로그인 코드는 **발급 후 5분간 한 번** 사용할 수 있습니다. 새 세션과 브라우저 쿠키는 **로그인부터 48시간(2일)** 유효하며 요청마다 연장하지 않습니다. 로그아웃하면 즉시 무효화합니다. 기존 코드·세션의 만료 기록은 정책 변경으로 소급 변경하지 않습니다. 로그인 코드·쿠키·CSRF 토큰은 공개 문서나 URL·로그에 넣지 마세요.

## 설정과 CLI 실행

```sh
cp config.example.yaml config.yaml
```

GUI의 프로젝트 등록·설정에서 **검증 자동 감지**를 누르면 기준 브랜치에 커밋된 설정을 읽어 Python pytest, Node scripts·Playwright, Java Maven·Gradle 검증 후보를 표시합니다. 실제 명령과 감지 근거를 확인하고 필요한 검증을 선택해 적용한 뒤 저장하세요. 감지는 명령 실행이나 의존성 설치를 하지 않으며 미커밋 설정은 사용하지 않습니다. 여러 언어·하위 프로젝트도 각각 작업 경로를 표시합니다. 감지가 어려우면 **도구 직접 선택**으로 시작하고 실행 파일·인자·JUnit·시간·허용 경로를 폼에서 수정할 수 있습니다. JSON은 고급 설정에 있습니다.

실행 미리보기는 저장된 pytest·Node·Maven·Gradle 정책과 감지된 도구가 다르면 설정 변경 위치를 안내합니다. 사용자 정의 명령의 의미까지 자동 판별하지는 않습니다.

Node는 실제 등록된 scripts만 추천하며 npm·pnpm·Yarn을 구분합니다. Vitest 스크립트에는 단발 실행·JUnit 인자를 제안하고 Playwright에는 JUnit 출력 파일을 지정합니다. 다른 Node 도구는 해당 스크립트에서 JUnit을 생성하도록 준비해야 합니다. Java는 Wrapper가 있으면 우선 사용하며 Maven Surefire·Gradle의 파일별 `TEST-*.xml` 보고서를 수집합니다. 사용자 정의 테스트 태스크·멀티모듈 설정은 실제 명령과 출력 경로를 확인하세요.

CLI에서는 설정의 `tests.commands`를 대상 프로젝트에 맞게 수정한 뒤 실행합니다. 별도 설정 파일을 전달하지 않은 내장 기본값에는 테스트 명령이 없으므로 작업 실행 전에 설정이 필요합니다.

- `tests.commands`: 호스트가 신뢰하는 명령 ID와 `argv` 인자 배열, 제한 시간을 지정합니다. `kind: test`(기본값)는 단일 파일 또는 glob인 `junit_report`에 매번 새 JUnit XML을 생성해야 합니다. `kind: check`는 빌드·타입·정적 검사를 종료 코드로 판정하며 JUnit을 지정하지 않습니다. 테스트 명령은 반드시 하나 이상 있어야 하고 모든 선택 명령이 통과해야 합니다.
- `working_directory`: 명령을 실행할 저장소 기준 상대 경로이며 기본값은 `.`입니다. `junit_report`, `tests.write_paths`, `tests.output_paths`는 작업 경로와 관계없이 저장소 루트를 기준으로 지정합니다.
- `network`: 기본값 `none`은 네트워크를 차단합니다. macOS의 `loopback`은 브라우저 검사·Gradle 내부 통신에 필요한 localhost 통신을 허용하고 외부 네트워크는 차단합니다. 서버 주소는 `127.0.0.1`을 사용하세요. Linux의 loopback 모드는 모델 호출 전에 실행을 거부하며 일반 오프라인 검증은 기존 격리를 유지합니다.
- `env`: 명령별 `JAVA_HOME`, `PLAYWRIGHT_JUNIT_OUTPUT_FILE`, `PLAYWRIGHT_BROWSERS_PATH`만 지정할 수 있습니다. JDK·의존성·브라우저는 미리 준비하세요. Java 예시는 Worktree의 `.devorchestra-cache`를 사용하며 Wrapper 배포본·오프라인 캐시도 필요합니다.
- `tests.write_paths`: Test Agent가 새 테스트 파일을 추가할 수 있는 경로입니다. 기존 테스트 파일 수정은 허용하지 않습니다.
- `tests.output_paths`: 테스트 격리 중 쓰기를 허용하는 빌드·보고서 경로입니다. Git이 추적하는 소스 파일이 포함된 디렉터리는 허용하지 않습니다.
- `storage`: 기본 위치는 저장소 외부의 `~/.devorchestra`입니다. DB·실행 산출물·Worktree를 대상 저장소 내부에 두면 실행을 거부합니다. 상대 저장 경로는 YAML 파일 위치를 기준으로 해석합니다.

저장한 정책은 **새 작업**에 적용됩니다. 기존 작업은 생성 당시의 검증 계약을 보존하며, 재개 답변으로 명령이 바뀌지 않습니다. 정책 불일치가 있으면 작업 화면의 **프로젝트 검증 설정 열기**에서 수정한 뒤 **수정한 정책으로 새 작업 만들기**를 사용하세요. 이전 작업의 변경 내용과 이력은 보존되며 새 작업으로 자동 이전하지 않습니다.

테스트는 셸 없이 실행하고 외부 네트워크를 차단합니다. 필요한 의존성은 실행 전에 오프라인에서 사용할 수 있도록 준비하세요. Maven·Gradle·JavaScript 도구도 JUnit XML을 생성하면 사용할 수 있지만, 프로젝트별 캐시·출력 경로 설정이 필요할 수 있습니다. 대상 프로젝트 의존성을 자동 설치하지 않습니다. 테스트 0건, 전체 건너뛰기, 보고서 누락·형식 오류를 통과로 처리하지 않습니다.

`--config`·`--database`는 하위 명령 앞에 지정합니다. 아래 예시는 모든 명령에서 같은 설정을 사용합니다.

```sh
devorchestra --config config.yaml doctor
devorchestra --config config.yaml run --repo ~/projects/example --spec PRD.md --base main --max-fix-attempts 3
# CLI 종료 후에도 실행하려면 run에 --detach를 지정합니다.
devorchestra --config config.yaml status RUN_ID
devorchestra --config config.yaml list
devorchestra --config config.yaml logs RUN_ID --follow --role reviewer
devorchestra --config config.yaml pause RUN_ID
devorchestra --config config.yaml resume RUN_ID --answers answers.md --max-fix-attempts 5
devorchestra --config config.yaml cancel RUN_ID
devorchestra --config config.yaml report RUN_ID
devorchestra --config config.yaml approve RUN_ID
devorchestra --config config.yaml cleanup RUN_ID
```

`doctor`는 모델 호출 없이 Codex 명령 호환성과 OS 테스트 샌드박스를 확인합니다. 조회 명령은 JSON을 출력하며 `logs`는 로그, `report`는 보고서 본문을 출력합니다. 일반 실행에서 `Ctrl+C`는 안전한 일시중지를 요청합니다. 취소는 하위 프로세스 종료를 확인한 뒤 확정합니다.

| 상황 | 종료 코드 |
|---|---|
| 일반 완료·최종 승인 대기 도달 | `0` |
| 명령·입력 오류 | `1` |
| 포그라운드 `run`·`resume`의 실행 실패 또는 사용자 개입 필요 | `2` |

## 워크플로와 승인

```text
queued → preparing → planning → developing → testing → reviewing
                                                         ↓
                        waiting_approval ← reporting ← 검증 통과
```

테스트 실패나 차단 리뷰 이슈가 있으면 `fixing → testing → reviewing`을 반복합니다. 자동 수정은 기본 최대 3회이며, `needs_input`은 질문 답변·수정 한도 상향 같은 사용자 판단이 필요한 상태입니다. `resume`은 검증된 체크포인트를 보존하며 수정 횟수를 초기화하지 않습니다.

각 역할은 새 읽기 전용 Codex 세션에서 구조화된 결과를 반환합니다. 호스트가 unified diff를 검증·적용하며 Developer·Fix Agent는 기존 테스트를 수정할 수 없습니다. Test Agent도 기존 테스트를 수정할 수 없으며 새 파일로 추가해야 합니다. 파일 삭제·이름 변경, 바이너리 패치, 심볼릭 링크, 권한 변경은 자동 적용하지 않습니다. 자동 수정 중 Planning에서 확정한 인수 조건을 바꾸지 않습니다.

승인은 현재 HEAD·작업 트리·테스트·리뷰·체크포인트와 검증 산출물의 무결성이 모두 확인된 후보에만 적용합니다. 리뷰 V2의 이슈 해결에는 같은 후보 SHA·revision의 새 테스트와 명시적 재검증 근거가 필요하며, 이전 리뷰에서 빠졌다고 자동 해결하지 않습니다. 차단 이슈는 사용자 분류로 우회할 수 없습니다.

웹용 스키마 5에서는 승인 대기 중 수정 요청과 승인 보류를 CLI에서도 사용할 수 있습니다.

```sh
devorchestra --config config.yaml request-changes RUN_ID --instructions changes.md --detach
devorchestra --config config.yaml defer RUN_ID --reason "추가 확인 후 승인"
```

수정 요청은 새 revision에서 Fix·Test·Review를 다시 수행하며 변경이 없는 Fix도 새 검증을 거칩니다. 수정 한도는 유지하고 필요하면 `--new-max-fix-attempts`로 상향합니다. 승인은 특정 SHA의 이력이며, 이후 변경된 승인 결과는 `status`에서 사용자 개입이 필요한 상태로 표시합니다.

`cleanup`은 Worktree만 정리하고 브랜치·커밋·보고서·SQLite 이력을 보존합니다. 미커밋 변경이 있는 Worktree를 강제로 삭제하지 않습니다. 보존된 Diff·JUnit·보고서는 Worktree 정리 후에도 조회할 수 있습니다.

## API·Dispatcher와 GUI 개발

CLI 기본 DB는 스키마 2를 사용하며 웹·큐는 스키마 5가 필요합니다. API 시작만으로 기존 DB를 자동 업그레이드하지 않습니다. 아래 명령은 같은 `config.yaml`을 공유합니다.

```sh
# 최초 웹 사용 또는 구버전 DB 업그레이드 시 실행합니다.
devorchestra --config config.yaml database-upgrade

# API와 GUI 개발 서버는 각각 별도 터미널에서 실행합니다.
devorchestra --config config.yaml api-start --allow-root ~/projects
devorchestra --config config.yaml worker start --detach
devorchestra --config config.yaml web-session

npm ci
npm run dev:web
```

마이그레이션 전에 API·웹 서비스·Dispatcher·Run Worker 등 DB writer를 중지하세요. 실행 중 Worker·미확인 프로세스·잠금이 있으면 거부하며, 스키마 2·3·4의 SQLite 백업과 증거 참조 manifest를 DB 옆에 보존한 뒤 트랜잭션으로 업그레이드합니다. 구버전 실행 파일과 혼용하지 말고 복원도 모든 writer가 종료된 상태에서 수행하세요.

API 기본 주소는 `http://127.0.0.1:8000`, Vite GUI는 `http://127.0.0.1:5173`입니다. `localhost`로 바꾸지 마세요. API 포트를 바꿨다면 `DEVORCHESTRA_API_PORT=8001 npm run dev:web`처럼 지정합니다. API는 loopback에만 바인딩하고 세션·정확한 Origin/Host·쓰기 CSRF를 검사합니다. 프로젝트는 `--allow-root` 아래 Git 저장소만 등록할 수 있으며 등록 해제는 원본 저장소를 삭제하지 않습니다.

API는 작업을 `queued`로 접수하고 독립 Dispatcher가 실행합니다. Dispatcher가 없으면 `queued_reason=dispatcher_offline`, 다른 작업이 실행 중이면 `waiting_capacity`를 반환합니다. `worker status`로 확인하고 `worker stop`으로 새 작업 실행을 중지할 수 있습니다. Dispatcher 종료와 API·SSE 연결 종료는 실행 중 Run 취소를 의미하지 않습니다. 미등록 CLI 작업은 GUI에서 조회 전용입니다.

SSE는 영속 event ID로 재연결·재생하며 GUI는 연결 단절 중 이력을 주기적으로 조회합니다. 로그 화면은 가상 목록과 최대 1,000개 구간·1 MiB 보관 한도를 사용합니다. 화면 로그 저장은 현재 보관 구간이며 전체 로그는 별도 다운로드를 사용합니다. 인증된 `/api/openapi.json`·`/api/docs`는 OpenAPI JSON을 제공하고 외부 CDN의 Swagger UI는 사용하지 않습니다. 화면과 프론트엔드 세부 사항은 [웹 앱 안내](apps/web/README.md)를 참고하세요.

## 정적 GUI와 배포

```sh
npm ci
.venv/bin/python scripts/build_web.py
uv build
```

빌드 도구는 `apps/web/dist/`를 `src/dev_orchestra/_web/`로 복사하고 체크섬 manifest를 생성합니다. GUI가 포함된 wheel을 설치한 환경에서는 Node 없이 Python의 `web` extra로 실행할 수 있습니다. 소스 저장소의 `start.sh`는 매번 빌드하므로 Node가 필요하고, 설치된 패키지는 다음 통합 명령을 사용합니다.

```sh
devorchestra --config config.yaml database-upgrade
devorchestra --config config.yaml web start --detach --allow-root ~/projects --port 8000
devorchestra --config config.yaml web-session
devorchestra --config config.yaml web status
devorchestra --config config.yaml web stop
```

GUI와 API를 `http://127.0.0.1:8000`에서 함께 제공합니다. GUI 파일이 없는 CLI 전용 배포도 지원하며 `web start`에서 빌드 필요성을 안내합니다. `web start`는 정적 파일 체크섬·포트·실행 소유권을 확인합니다. `web stop --cancel-active`는 같은 DB의 미완료 작업 취소까지 요청합니다.

## 저장소 구조

| 경로 | 역할 |
|---|---|
| `src/dev_orchestra/domain/`, `src/dev_orchestra/config.py` | 도메인 모델·설정 검증 |
| `src/dev_orchestra/application/` | CLI·API 공통 실행·조회·프로젝트·큐·결과·승인 서비스 |
| `src/dev_orchestra/orchestration/`, `src/dev_orchestra/agents/` | 워크플로 실행·역할 프롬프트·패치 정책 |
| `src/dev_orchestra/infrastructure/` | Git·SQLite·샌드박스·프로세스 관리·검증 근거 |
| `src/dev_orchestra/cli.py`, `src/dev_orchestra/api/`, `src/dev_orchestra/execution/`, `src/dev_orchestra/worker.py` | CLI·HTTP·SSE 어댑터와 독립 실행 프로세스 |
| `apps/web/` | React + TypeScript + Vite GUI |
| `tests/`, `apps/web/tests/`, `apps/web/e2e/` | Python·화면·브라우저 검사 |
| `scripts/` | GUI 빌드·API 생성·배포 검사·벤치마크·선택 E2E |
| `start.sh`, `stop.sh` | 소스 저장소의 반복 기동·종료 |

공통 서비스는 FastAPI·Typer에 의존하지 않습니다. 읽기 전용 조회는 복구·보고서 생성을 수행하지 않으며 상태 조정은 별도 경로로 처리합니다. 기존 CLI `status`는 복구 동작을 유지합니다. 기여 시에는 [저장소 작업 규칙](AGENTS.md)을 따르세요.

## 개발 및 검증

저장소 루트에서 웹 개발 환경과 npm 의존성을 준비한 뒤 필요한 범위의 검사를 실행합니다. 아래 일반 검사는 실제 모델을 호출하지 않습니다.

```sh
.venv/bin/python -m pytest -q
.venv/bin/ruff check src tests scripts apps/web/e2e/backend.py
.venv/bin/ruff format --check src tests scripts apps/web/e2e/backend.py
npm run test:web
npm run typecheck:web
npm run format:check --workspace apps/web

# 공개 API 계약과 생성 타입의 일치 확인
.venv/bin/python scripts/export_web_schema.py --check
npm run check:api --workspace apps/web

# 실제 OS 샌드박스 검사와 로그 재생 벤치마크
DEVORCHESTRA_TEST_SANDBOX=1 .venv/bin/python -m pytest tests/test_security.py -q

# macOS: 설치된 Chromium으로 실제 브라우저 격리·JUnit 수집 검사 (선택)
DEVORCHESTRA_TEST_BROWSER_SANDBOX=1 .venv/bin/python -m pytest tests/test_validation_policy.py -k actual_playwright -q
.venv/bin/python scripts/benchmark_logs.py
```

API 계약을 변경했다면 `.venv/bin/python scripts/export_web_schema.py`와 `npm run generate:api --workspace apps/web`을 차례로 실행해 OpenAPI와 TypeScript 타입을 함께 갱신합니다.

```sh
# 실제 브라우저·임시 Git/SQLite·Mock Codex·실제 pytest
npx playwright install chromium --only-shell
npm run test:e2e --workspace apps/web

# Python이 제공하는 정적 GUI에서도 같은 흐름 검사
.venv/bin/python scripts/build_web.py
E2E_BUNDLED=1 npm run test:e2e --workspace apps/web
```

브라우저 검사는 5173·18080 포트를 사용하며 기존 서버를 재사용하지 않습니다. 테스트 상태는 임시 저장소·DB에 격리합니다. 검사 산출물과 실패 trace에는 테스트용 세션 정보가 포함될 수 있으므로 외부 공유 전 확인하세요.

wheel을 각각 설치한 웹 환경(`web` extra 포함)과 CLI 전용 환경을 준비했다면 다음 검사로 Node 없는 GUI와 웹 의존성 없는 CLI를 확인할 수 있습니다.

```sh
.venv/bin/python scripts/verify_package.py --web-python /absolute/path/to/web-env/bin/python --core-python /absolute/path/to/core-env/bin/python
```

다음 E2E는 실제 Codex를 호출하고 사용량이 발생합니다. 사용자 승인 후 선택 실행하며 일반 테스트·CI에서 자동 실행하지 마세요.

```sh
.venv/bin/python scripts/e2e.py
.venv/bin/python scripts/e2e_gui.py --allow-model-calls --model MODEL_ID
```

GUI 모델 검사는 Developer 응답에 명시적인 결함을 주입하고 원래 응답을 보존한 뒤 Fix·Test·Reviewer V2 재검증·CLI 승인을 확인합니다. 실행당 최대 7회 호출, 역할별 제한 시간 180초를 사용합니다. 개별 테스트 통과는 지원 범위 밖 환경이나 장시간 장애 복구까지 보장하지 않습니다.

## 안전성과 운영 제약

- **원본과 Git**: 원래 작업 디렉터리의 수정 내용·현재 브랜치를 보존하고 기준 커밋에서 Worktree를 만듭니다. 호스트 Git 명령은 hooks·서명·fsmonitor를 비활성화합니다. 실행 가능한 clean/smudge/process 필터가 있는 저장소는 거부하므로 Git LFS 프로젝트도 해당할 수 있습니다.
- **샌드박스**: 테스트 소스는 읽기 전용이며 지정 출력 경로·임시 HOME·선별 환경변수를 사용합니다. macOS는 알려진 인증 경로·프로젝트의 환경 파일·키 읽기를 차단하지만 모든 호스트 비밀 파일을 격리하지는 못합니다. Codex 읽기 전용 세션도 관련 없는 모든 호스트 파일의 읽기까지 막지는 않으므로 신뢰할 수 없는 저장소는 전용 계정·컨테이너에서 실행하세요.
- **복구와 소유권**: Worker 상실 시 별도 관리 프로세스가 자식 명령을 정리합니다. PID·생성 시각·실행 잠금으로 소유권을 확인하고 디스크의 PID만으로 다른 프로세스를 종료하지 않습니다. 종료 확인 실패, 외부 HEAD 변경, 미커밋 변경, 커밋 직후 체크포인트 누락은 수동 확인이 필요합니다. 자동 reset·clean으로 복구하지 않습니다.
- **동시성**: SQLite는 WAL·외래 키·짧은 트랜잭션을 사용하고 DB별 Worker 하나만 실행합니다. 다른 DB가 같은 Git common directory를 사용해도 저장소 잠금으로 직렬화합니다. heartbeat 지연만으로 살아 있는 Worker를 재실행하지 않습니다.
- **검증의 한계**: PRD·저장소 파일은 호스트 명령 선택이나 승인을 바꿀 수 없습니다. 프롬프트 인젝션이나 테스트 의미 약화를 완전히 자동 탐지할 수는 없고 모델의 요구사항 대응표도 충족의 증명은 아닙니다. 승인 후 파일시스템 감시는 없으므로 `status`로 유효성을 확인하세요.
- **민감정보 보관**: 로그는 마스킹하며 프로세스 출력은 최대 8 MiB입니다. 모든 비밀 형식을 식별할 수 없고 PRD·설정·구조화 응답·원본 진단 로그에는 프로젝트 정보가 남을 수 있습니다. 기본 상태 디렉터리 `~/.devorchestra`를 비공개로 관리하고 로그인 코드·세션·키·DB·로그를 커밋하지 마세요.
- **산출물과 전원**: 실행 자료는 설정된 `runs_directory`의 `RUN_ID/iterations/ITERATION/ROLE/attempt-NNN/`에 저장하고 실행 루트에 스냅샷·`final-report.md`를 보존합니다. 장시간 작업 중에는 컴퓨터가 실행 가능한 상태를 유지해야 하며 절전·세션 중단 뒤 모든 복구가 검증된 것은 아닙니다.
