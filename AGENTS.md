# 저장소 작업 규칙

## 언어와 개인정보

- 커밋 메시지는 한국어로 작성합니다.
- README.md와 docs/의 프로젝트 문서 설명은 한국어로 유지합니다. 명령어, 옵션, 경로 및 코드 식별자는 원래 표기를 유지합니다.
- README.md에는 실명, 개인 계정명·이메일, 사용자별 절대 경로, 비공개 저장소 주소·내부 접속 정보, 실제 로그인 코드·토큰·쿠키·키 등 개인·민감정보를 포함하지 않습니다. 예시는 `~/projects/example`, `/absolute/path/to/project`, `RUN_ID`처럼 일반화합니다. 로컬 접속 안내에는 `127.0.0.1`을 사용합니다.
- 실행 로그나 검증 결과를 문서에 옮길 때도 개인·민감정보를 제거합니다. 과거 테스트 수치나 의존성 감사 결과를 현재 결과처럼 기재하지 않습니다.

## 저장소 구조

- `src/dev_orchestra/cli.py`: Typer CLI와 `devorchestra`·`codex-pipeline` 진입점입니다.
- `src/dev_orchestra/domain/`, `src/dev_orchestra/config.py`: 도메인 모델과 YAML 설정 검증입니다.
- `src/dev_orchestra/application/`: CLI·API가 공유하는 실행·조회·프로젝트·큐·결과·승인 서비스입니다.
- `src/dev_orchestra/orchestration/`, `src/dev_orchestra/agents/`: 단계 실행과 Codex 프롬프트·편집 정책입니다.
- `src/dev_orchestra/infrastructure/`: Git·SQLite·마이그레이션·샌드박스·프로세스 소유권·검증 근거 저장입니다.
- `src/dev_orchestra/api/`, `src/dev_orchestra/execution/`, `src/dev_orchestra/worker.py`: FastAPI·SSE와 웹 서비스·Dispatcher·Run Worker입니다.
- `apps/web/`: React + TypeScript + Vite 기반 npm workspace입니다.
- `tests/`, `apps/web/tests/`, `apps/web/e2e/`: Python·화면 단위·브라우저 검사입니다.
- `scripts/`: GUI 빌드·API 스키마 내보내기·패키지 검사·로그 벤치마크·선택 E2E 도구입니다.
- `start.sh`, `stop.sh`: 저장소의 `.venv`로 로컬 GUI를 기동·종료합니다.
- `docs/`는 `.gitignore`에 등록된 로컬 문서입니다. 명시적 요청 없이 강제로 추적하거나 공개 README가 이 문서에 의존하도록 만들지 않습니다.

## 환경과 검증

명령은 저장소 루트에서 실행합니다. Python 3.11 이상을 사용하며, GUI 소스 개발·빌드에는 Node 22.12 이상과 npm이 필요합니다. `.tools/uv`나 `.python`이 준비되어 있다고 가정하지 말고 표준 `uv` 명령을 안내합니다.

```sh
uv sync --frozen --extra web
npm ci

# Python 변경
.venv/bin/python -m pytest -q
.venv/bin/ruff check src tests scripts apps/web/e2e/backend.py
.venv/bin/ruff format --check src tests scripts apps/web/e2e/backend.py

# 프론트엔드 변경
npm run test:web
npm run typecheck:web
npm run format:check --workspace apps/web

# 공개 API와 생성 타입의 일치 확인
.venv/bin/python scripts/export_web_schema.py --check
npm run check:api --workspace apps/web
```

- 변경 범위에 맞는 검사를 실행합니다. 문서만 변경했다면 명령·옵션·링크·개인정보와 `git diff --check`를 확인하며 전체 제품 검사를 반복할 필요는 없습니다.
- 공개 API 계약을 변경하면 `.venv/bin/python scripts/export_web_schema.py`와 `npm run generate:api --workspace apps/web`으로 스키마·타입을 함께 갱신합니다. `apps/web/src/api/generated.ts`를 직접 수정하지 않습니다.
- 통합 GUI 빌드는 `.venv/bin/python scripts/build_web.py`를 사용합니다. 생성된 `apps/web/dist/`, `src/dev_orchestra/_web/`, `dist/`와 캐시·DB·실행 산출물은 커밋하지 않습니다.
- 실제 OS 샌드박스 검사는 `DEVORCHESTRA_TEST_SANDBOX=1 .venv/bin/python -m pytest tests/test_security.py -q`로 선택 실행합니다.
- 브라우저 검사는 Chromium을 설치한 뒤 `npm run test:e2e --workspace apps/web`으로 실행합니다. 정적 GUI 검사는 먼저 통합 GUI를 빌드하고 `E2E_BUNDLED=1`을 지정합니다. 검사는 5173·18080 포트를 사용하며 기존 서버를 재사용하지 않습니다.
- `scripts/e2e.py`와 `scripts/e2e_gui.py`는 실제 모델을 호출하고 사용량이 발생합니다. 사용자가 모델 호출을 명시적으로 승인한 경우에만 실행하며 일반 테스트에 포함하지 않습니다.

## 구현 시 유지할 경계

- 워크플로 판단은 공통 서비스·Runner에 둡니다. CLI·API는 어댑터이며 프론트엔드에서 Git이나 Codex를 직접 실행하지 않습니다. 읽기 전용 조회에 복구·보고서 생성 같은 변경 작업을 추가하지 않습니다.
- 실행 계약은 `workspace-v2`만 지원합니다. 구계약 `patch-v1`과 계약 식별자가 없는 Run은 이력·산출물을 보존하되 실행을 차단하며 자동 전환하지 않습니다. 모델 응답의 패치를 적용하는 경로를 추가하지 않습니다. 스키마 6에서 명시적으로 활성화한 `workspace-v2`의 Developer/Fixer는 읽기 전용 셸과 제한된 호스트 편집 도구로 실제 파일을 수정하고 작업 단위 커밋을 요청합니다. Tester는 `tests.write_paths` 안의 테스트만 호스트 도구로 편집하고 호스트의 실제 테스트 결과를 확인합니다. 호스트만 선택 파일을 검증·커밋하며 Planner/Reviewer는 계속 읽기 전용입니다. Reviewer의 근거 도구는 해당 후보의 테스트 결과 읽기만 허용합니다. 신규 계약은 `database-upgrade --workspace`의 백업·종료 검사와 스키마 6 경계를 거쳐 새 Run에만 활성화합니다. 스키마 2·5에서는 새 Run을 생성·실행하지 않으며 명시적인 스키마 6 전환을 요구합니다. 기존 테스트 보호, 테스트 명령 허용 목록, JUnit 검증, SHA·revision·체크섬에 연결된 승인 근거를 약화하지 않습니다.
- 테스트 범위(`full`/`affected`)와 영향 선택 규칙은 생성 시 고정합니다. 호스트가 누적 변경·의존 관계·필수 회귀·인수 조건에 따라 명령을 선택하고 불확실하면 전체 검사로 확대합니다. 테스트·리뷰·최종 승인 근거는 동일 SHA·revision·선택 정책에 연결하며 재개 답변으로 범위나 명령을 변경하지 않습니다.
- `workspace-v2`의 중단 복구는 종료·소유권 확인 뒤 DB가 참조하는 불변 스냅샷과 실제 파일·Git 이력·정책·요구사항이 일치할 때만 같은 역할·시도를 이어갑니다. 미확인 변경은 보존·차단합니다. Developer/Fixer는 해당 단계에 연결된 명시적 Codex 세션 ID만 재개하며 `--last`를 사용하지 않습니다. 대화 세션이 없어도 검증된 파일과 요약으로 계속할 수 있습니다. 메타데이터만 복구할 때는 편집·커밋·테스트 실행을 잠그고 유효한 실제 근거를 재사용합니다. 재개·중간 커밋으로 응답 복구 한도나 수정 회차를 초기화하지 않으며 표시용 패치를 소스 복원에 사용하지 않습니다.
- 병렬 실행은 독립 Run 단위입니다. DB·Git common directory별 기본 한도는 1이며 `worker capacity`의 공통 정책과 슬롯을 사용합니다. `claimed`도 용량에 포함하고 미확인 자식 프로세스가 남은 슬롯·loopback·쓰기 캐시는 재사용하지 않습니다. 잠금 순서는 유지보수 → Run 실행 → 구버전 호환 게이트 → DB 슬롯 → 저장소 슬롯 → 짧은 Git 메타데이터/공유 자원 잠금입니다. 모델·테스트 호출 동안 DB 트랜잭션이나 배타 저장소 잠금을 유지하지 않습니다. 기존 `.worker.lock`·`devorchestra.lock`은 새 Worker의 공유 호환 게이트이며 구버전 배타 Worker와 병렬 실행하지 않습니다.
- 대상 프로젝트의 자동 Push·Merge를 추가하지 않습니다. 원본 작업 디렉터리의 미커밋 변경을 보존하고, 복구를 위해 자동 reset·clean이나 강제 Worktree 삭제를 수행하지 않습니다.
- 프로세스 종료는 PID만으로 판단하지 말고 생성 시각·실행 잠금·소유권을 확인합니다. API·SSE·Dispatcher 종료를 개별 Run 취소로 해석하지 않습니다.
- 웹은 loopback·Origin/Host·세션·CSRF 검사를 유지합니다. 로그인 코드·세션·CSRF를 URL, 로그, localStorage에 저장하지 않습니다. 현재 코드 유효기간은 5분, 새 세션 수명은 로그인부터 48시간입니다.
- DB 초기화는 스키마 2를 유지하며 웹·큐의 이력 조회는 스키마 5·6을 사용합니다. 새 Run 생성·실행은 스키마 6에서만 허용합니다. 스키마 6은 구버전 실행 파일이 거부하는 직접 편집 활성화 경계이며 스키마 5의 테이블 구성을 재사용합니다. 마이그레이션은 기존 안전 검사·백업·트랜잭션을 유지하고 사용자 운영 DB로 개발 검사를 수행하지 않습니다.
- 커밋 전 `git status --short`와 `git diff --check`를 확인하고 요청한 변경만 스테이징합니다.
