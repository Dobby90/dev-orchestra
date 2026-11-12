# DevOrchestra

Codex CLI 기반의 로컬 개발 오케스트레이션 도구입니다. Git 저장소와 요구사항 문서(PRD)를 입력하면 기획, 개발, 테스트, 독립 코드 리뷰, 자동 수정을 진행하고 사용자의 최종 승인을 기다립니다.

작업마다 별도 브랜치와 Git Worktree를 사용하며, CLI와 웹 GUI에서 같은 실행 상태와 검증 결과를 확인할 수 있습니다.

- **역할별 작업**: Planner, Developer, Tester, Reviewer, Fixer가 기획부터 수정까지 나누어 수행합니다.
- **실제 검증 근거**: 호스트가 허용한 테스트 명령을 실행하고 JUnit 결과·리뷰·Diff·보고서를 보존합니다.
- **중단과 재개**: 작업을 일시중지하거나 질문에 답해 이어갈 수 있고, 브라우저를 닫아도 실행은 유지됩니다.
- **사용자 승인**: 개발 전 기획을 승인하고, 검증된 최종 커밋을 검토한 뒤 결과를 승인합니다.

## 요구 환경

| 용도 | 요구 사항 |
| --- | --- |
| 공통 | Python 3.11 이상, Git, 인증된 Codex CLI |
| Python 환경 준비 | `uv` |
| 테스트 격리 | macOS Seatbelt 또는 Linux `bwrap` |
| 웹 GUI/API | Python의 `web` extra |
| GUI 소스 실행·빌드 | Node 22.12 이상, npm |

주 사용 환경은 macOS의 단일 사용자·신뢰하는 로컬 프로젝트입니다. Linux `bwrap`과 다른 브라우저 환경은 별도 검증이 필요하며, Windows·원격 실행·다중 사용자 운영은 지원하지 않습니다. Git은 `add --pathspec-from-file`을 지원해야 합니다.

실제 작업 실행에는 Codex 모델 사용량이 발생합니다. 대상 프로젝트의 언어 런타임과 테스트 도구도 준비되어 있어야 합니다.

## 빠른 시작

아래 명령은 이 저장소를 내려받은 뒤 **저장소 루트**에서 실행합니다. `~/projects`는 등록할 프로젝트가 들어 있는 실제 상위 디렉터리로 바꾸세요.

### 1. 환경 준비

```sh
uv sync --frozen --extra web
codex login
```

### 2. 실행용 DB 활성화

처음 사용하기 전에 직접 편집 실행을 활성화합니다. 기존 DB를 사용한다면 API·웹 서비스·Dispatcher·Run Worker를 모두 종료한 상태에서 실행하세요.

```sh
.venv/bin/devorchestra database-upgrade --workspace
```

이 명령은 기존 DB의 백업과 검증 근거 참조를 보존하고 스키마 6으로 전환합니다. 새 작업 생성·실행에는 이 전환이 필요하며, `start.sh`나 일반 `database-upgrade`가 자동으로 활성화하지 않습니다.

### 3. GUI 시작

```sh
./start.sh --allow-root ~/projects
```

`start.sh`는 필요한 npm 의존성을 설치하고 최신 GUI를 빌드한 뒤 GUI·API·작업을 배정하는 Dispatcher를 백그라운드로 시작합니다. 상태는 SQLite에 저장하므로 별도 DB 서버가 필요하지 않습니다.

출력된 [http://127.0.0.1:8000](http://127.0.0.1:8000)에 접속해 터미널에 표시된 일회용 로그인 코드를 입력하세요. 코드는 발급 후 5분간 한 번 사용할 수 있으며, 로그인 세션은 로그인부터 48시간 유효합니다.

### 4. 첫 작업 실행

1. **프로젝트**에서 Git 저장소와 기준 브랜치를 등록합니다.
2. **검증 자동 감지**로 테스트·빌드 후보를 확인하고 실행 명령과 출력 경로를 저장합니다. 직접 설정할 수도 있습니다.
3. **새 작업**에서 PRD를 작성하거나 파일로 올리고, 테스트 범위와 실행 미리보기를 확인해 시작합니다.
4. **기획안**에서 구현 계획과 인수 조건을 검토한 뒤 **기획 승인 후 개발 시작**을 선택합니다.
5. 테스트·리뷰·Diff·보고서를 확인하고 최종 승인합니다.

PRD에는 목표, 구현 범위, 제약 사항, 완료를 판단할 인수 조건을 적으면 좋습니다. 형식은 자유이며, 모호한 요구사항은 실행 중 추가 질문으로 확인할 수 있습니다.

### 종료와 다시 시작

```sh
./stop.sh
./start.sh --allow-root ~/projects

# 미완료 작업도 취소하며 종료
./stop.sh --cancel-active
```

일반 종료는 해당 서비스가 시작한 API·Dispatcher를 종료하며 실행 중 Run Worker는 유지합니다. 서비스를 다시 시작하면 기존 대기 작업도 실행할 수 있습니다.

| 시작 옵션 | 용도 |
| --- | --- |
| `--allow-root 경로` | 등록을 허용할 상위 경로. 여러 번 지정할 수 있습니다. |
| `--port 8001` | 접속 포트 변경. 기본값은 `8000`입니다. |
| `--config config.yaml` | CLI와 공유할 YAML 설정 지정. 종료에도 같은 파일을 지정합니다. |
| `--rebuild` | 서비스 종료 후 npm 의존성까지 다시 설치합니다. |

이미 실행 중이면 `start.sh`는 기존 서비스와 포트를 재사용하고 새 로그인 코드를 발급합니다. GUI 소스·포트·허용 경로 변경은 종료 후 다시 시작해야 반영됩니다.

## 작업 흐름과 결과

| 단계 | 수행 내용 |
| --- | --- |
| Planner | 요구사항을 분석하고 구현 계획·인수 조건을 작성합니다. |
| 기획 승인 | 사용자가 계획을 승인하거나 수정 요청을 보냅니다. |
| Developer | 전용 Worktree의 코드를 수정하고 작업 단위 커밋을 요청합니다. |
| Tester | 허용된 테스트 경로에서 테스트를 보완하고 실제 실행 결과를 확인합니다. |
| Reviewer | 읽기 전용으로 코드와 같은 후보의 테스트 근거를 독립 검토합니다. |
| Fixer | 테스트 실패나 차단 리뷰 이슈를 수정한 뒤 테스트·리뷰를 다시 거칩니다. |
| 최종 승인 | 사용자가 검증된 커밋 SHA의 결과를 승인합니다. |

자동 수정은 기본 최대 3회입니다. 추가 질문이나 한도 상향이 필요하면 입력을 기다리며, 재개해도 사용한 수정 횟수는 초기화되지 않습니다. 최종 승인 대기 중에도 수정 요청을 보내 다시 검증할 수 있습니다.

최종 승인은 특정 커밋에 대한 기록입니다. 승인으로 원본 브랜치에 자동 Merge하거나 원격 저장소에 자동 Push하지 않습니다. GUI에서는 프로젝트 Git 연결을 설정한 뒤 사용자가 **작업 브랜치 Push**를 실행할 수 있습니다. 원격 반영을 마친 뒤 Worktree를 정리해도 로컬 브랜치와 보고서는 보존됩니다.

## CLI 사용

GUI 없이 사용하려면 `uv sync --frozen`으로 Python 환경만 준비할 수 있습니다. `devorchestra`와 `codex-pipeline`은 같은 CLI이며, 다음 예시는 가상환경의 실행 파일을 직접 사용합니다.

먼저 [설정 예시](config.example.yaml)를 복사하고 대상 프로젝트의 테스트 명령·실행 파일 경로·JUnit 경로를 수정하세요.

```sh
cp config.example.yaml config.yaml
```

`--config`는 하위 명령 앞에 지정하며 모든 명령에 같은 파일을 사용합니다. DB 전환은 빠른 시작과 마찬가지로 관련 서비스를 모두 종료한 상태에서 진행합니다.

```sh
.venv/bin/devorchestra --config config.yaml database-upgrade --workspace
.venv/bin/devorchestra --config config.yaml doctor
.venv/bin/devorchestra --config config.yaml run \
  --repo ~/projects/example --spec PRD.md --base main --detach
```

`doctor`는 모델 호출 없이 Codex 명령 호환성과 OS 테스트 샌드박스를 확인합니다. `--detach`는 백그라운드 실행을 요청하며, 생략하면 해당 실행 시도가 끝나거나 승인·입력이 필요할 때까지 기다립니다.

출력된 작업 ID를 `RUN_ID`에 넣습니다. 기획 승인 대기의 `status`에서 계획·인수 조건과 `plan_hash`를 확인한 뒤 해당 값을 `PLAN_HASH`에 넣어 승인하세요.

```sh
.venv/bin/devorchestra --config config.yaml status RUN_ID
.venv/bin/devorchestra --config config.yaml logs RUN_ID --follow
.venv/bin/devorchestra --config config.yaml approve-plan RUN_ID --plan-hash PLAN_HASH --detach
```

기획을 수정하려면 `revise-plan RUN_ID --plan-hash PLAN_HASH --reason "수정할 내용"`을 사용합니다. 일반 `resume`은 기획 승인을 대신하지 않습니다.

최종 승인 대기에 도달하면 보고서와 테스트·리뷰·Diff를 검토한 뒤 승인합니다.

```sh
.venv/bin/devorchestra --config config.yaml report RUN_ID
.venv/bin/devorchestra --config config.yaml approve RUN_ID
```

| 명령 | 용도 |
| --- | --- |
| `list` | 전체 작업 조회 |
| `pause RUN_ID` | 일시중지 요청 |
| `resume RUN_ID --answers answers.md --detach` | 답변 파일을 전달해 재개 |
| `resume RUN_ID --max-fix-attempts 5 --detach` | 저장된 수정 한도를 상향해 재개 |
| `cancel RUN_ID` | 취소 요청 |
| `request-changes RUN_ID --instructions changes.md --detach` | 최종 승인 대기 중 수정 요청 |
| `cleanup RUN_ID` | Worktree 정리, 브랜치·보고서 보존 |
| `worker status` | Dispatcher 상태 조회 |

전체 옵션은 `.venv/bin/devorchestra --help`와 각 하위 명령의 `--help`에서 확인할 수 있습니다. CLI에서 만든 미등록 프로젝트의 작업은 GUI에서 조회 전용입니다.

## 설정

GUI의 프로젝트 설정이나 [config.example.yaml](config.example.yaml)을 기준으로 실행 정책을 구성합니다.

| 설정 | 의미 |
| --- | --- |
| `project.default_base_branch` | 기본 기준 브랜치 |
| `codex.model`, `codex.reasoning_effort` | 공통 모델과 추론 강도 |
| `codex.roles` | 역할별 모델·추론 강도 재정의 |
| `workflow.max_fix_attempts` | 자동 수정 한도 |
| `tests.commands` | 호스트가 실행할 명령의 인자·작업 경로·제한 시간·JUnit 경로 |
| `tests.write_paths` | Tester가 편집할 수 있는 테스트 경로 |
| `tests.output_paths` | 검증 명령이 산출물을 쓸 수 있는 경로 |
| `storage` | DB·실행 산출물·Worktree 저장 위치 |

검증 정책에는 JUnit XML을 만드는 `kind: test` 명령이 하나 이상 필요합니다. 빌드·타입 검사 등은 `kind: check`로 추가할 수 있습니다. 테스트 0건·전체 건너뛰기·보고서 누락은 통과로 처리하지 않습니다. 명령은 셸 문자열 대신 `argv` 배열로 지정합니다.

테스트 범위의 기본값은 전체 검사(`full`)입니다. 변경 영향 범위 검사(`affected`)는 누적 변경과 인수 조건에 따라 검사를 선택하고 호스트가 검증합니다. CLI에서는 `run --test-scope affected`로 지정합니다. 범위와 정책은 작업 생성 시 고정되며 이후 설정 변경은 새 작업에 적용됩니다.

GUI **설정 → 에이전트 모델 설정**에서 공통·역할별 모델을 지정하고 새 작업에서 해당 작업에만 적용할 값을 바꿀 수 있습니다. 저장한 GUI 모델 설정은 같은 DB를 사용하는 CLI의 새 작업에도 적용되며 YAML의 모델 설정보다 우선합니다.

기본 저장 위치는 `~/.devorchestra`입니다. 저장 위치는 대상 저장소 외부에 두어야 하며, YAML의 상대 저장 경로는 설정 파일 위치를 기준으로 해석합니다.

## 실행 경계와 주의 사항

- 원본 작업 디렉터리의 미커밋 변경과 현재 브랜치를 보존하고, 기준 커밋에서 전용 Worktree를 만듭니다.
- Developer/Fixer/Tester는 제한된 호스트 도구로 파일을 편집하고 호스트가 정책을 검증해 커밋합니다. Planner/Reviewer는 읽기 전용입니다.
- 테스트는 OS 샌드박스에서 실행하며 기본적으로 외부 네트워크를 차단합니다. 의존성 자동 준비 범위를 벗어나는 환경은 별도로 준비해야 합니다.
- 웹은 `127.0.0.1`에서 사용하며 세션·Origin/Host·CSRF 검사를 적용합니다. 외부 공개용 서버로 사용하지 마세요.
- 기본 동시 실행 한도는 DB 전체와 Git 공통 저장소별로 각각 1입니다. `worker capacity`로 독립 작업의 동시 실행 수를 조정할 수 있습니다.
- 중단 후 파일·Git 이력·실행 소유권이 기록과 일치할 때만 재개합니다. 미확인 변경은 보존하고 사용자 확인을 요청하며 자동 `reset`·`clean`으로 복구하지 않습니다.
- DB·PRD·로그·보고서에는 프로젝트 정보가 남을 수 있습니다. 상태 디렉터리를 비공개로 관리하고 로그인 코드·세션·키·실행 산출물을 커밋하지 마세요.

현재 실행 계약은 `workspace-v2`입니다. 구계약 `patch-v1`과 계약 식별자가 없는 작업은 이력·산출물을 보존하되 실행하지 않으므로 새 작업을 만들어야 합니다. DB 전환 전 백업은 보존하고 구버전 실행 파일과 혼용하지 마세요.

샌드박스와 자동 리뷰가 모든 비밀 파일 접근이나 검증 약화를 차단하는 것은 아닙니다. 신뢰하는 프로젝트에서 사용하고 최종 Diff와 검증 결과를 직접 확인하세요.

## 개발 및 검증

개발 환경은 저장소 루트에서 준비합니다. 일반 단위·통합 검사와 Mock Codex를 사용하는 브라우저 검사는 실제 모델을 호출하지 않습니다.

```sh
uv sync --frozen --extra web
npm ci

# Python
.venv/bin/python -m pytest -q
.venv/bin/ruff check src tests scripts apps/web/e2e/backend.py
.venv/bin/ruff format --check src tests scripts apps/web/e2e/backend.py

# 웹 화면
npm run test:web
npm run typecheck:web
npm run format:check --workspace apps/web

# API 스키마·생성 타입 일치
.venv/bin/python scripts/export_web_schema.py --check
npm run check:api --workspace apps/web
```

API 계약을 변경하면 `.venv/bin/python scripts/export_web_schema.py`와 `npm run generate:api --workspace apps/web`을 차례로 실행합니다. 생성된 `apps/web/src/api/generated.ts`는 직접 수정하지 않습니다.

실제 OS 샌드박스 검사와 브라우저 검사는 선택 실행합니다.

```sh
DEVORCHESTRA_TEST_SANDBOX=1 .venv/bin/python -m pytest tests/test_security.py -q

npx playwright install chromium
npm run test:e2e --workspace apps/web
```

브라우저 검사는 `5173`·`18080` 포트를 사용하며 기존 서버를 재사용하지 않습니다. 통합 GUI는 `.venv/bin/python scripts/build_web.py`로 빌드하고, 빌드된 화면의 브라우저 검사는 `E2E_BUNDLED=1 npm run test:e2e --workspace apps/web`으로 실행합니다. 빌드 생성물은 커밋하지 않습니다.

`scripts/e2e.py`와 `scripts/e2e_gui.py`는 실제 모델을 호출하므로 명시적으로 승인한 경우에만 실행합니다. 기여 시에는 [저장소 작업 규칙](AGENTS.md)을 따르세요.

## 저장소 구조

| 경로 | 역할 |
| --- | --- |
| `src/dev_orchestra/cli.py` | Typer CLI 진입점 |
| `src/dev_orchestra/domain/`, `src/dev_orchestra/config.py` | 도메인 모델·설정 검증 |
| `src/dev_orchestra/application/` | CLI·API 공통 실행·조회·승인 서비스 |
| `src/dev_orchestra/orchestration/`, `src/dev_orchestra/agents/` | 단계 실행·역할 프롬프트·편집 정책 |
| `src/dev_orchestra/infrastructure/` | Git·SQLite·샌드박스·검증 근거 관리 |
| `src/dev_orchestra/api/`, `src/dev_orchestra/execution/`, `src/dev_orchestra/worker.py` | HTTP·SSE·Dispatcher·Run Worker |
| `apps/web/` | React + TypeScript + Vite GUI |
| `tests/`, `apps/web/tests/`, `apps/web/e2e/` | Python·화면 단위·브라우저 검사 |
| `scripts/` | GUI 빌드·스키마 내보내기·검증 도구 |
| `start.sh`, `stop.sh` | 로컬 GUI 기동·종료 |
