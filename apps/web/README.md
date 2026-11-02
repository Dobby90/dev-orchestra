# DevOrchestra 웹 화면 — Phase 5

React + TypeScript + Vite 기반 로컬 작업 공간입니다. Python 공통 서비스·SQLite·독립 Worker를 기존 API로 사용하며, 프론트엔드가 Codex나 Git을 직접 실행하지 않습니다.

## 실행

Node 22.12 이상과 npm, Python의 `web` extra가 필요합니다. 저장소 루트에서 실행하세요.

```sh
npm ci
UV_CACHE_DIR="$PWD/.uv-cache" .tools/uv sync --frozen --extra web
.venv/bin/devorchestra --config config.yaml database-upgrade
```

같은 설정 파일로 각 서비스를 별도 터미널에서 시작합니다.

```sh
.venv/bin/devorchestra --config config.yaml api-start --allow-root /Users/me/projects
.venv/bin/devorchestra --config config.yaml worker start --detach
npm run dev:web
```

`http://127.0.0.1:5173`에 접속하고 `devorchestra --config config.yaml web-session`으로 발급한 일회용 코드를 입력합니다. 원격 주소나 `localhost`로 바꾸지 마세요. 로그인 코드·세션 쿠키·CSRF는 URL이나 localStorage에 저장하지 않습니다. 테마 선택만 localStorage에 보관합니다.

기본 API는 `127.0.0.1:8000`입니다. 다른 로컬 포트로 API를 시작한 경우 `DEVORCHESTRA_API_PORT=포트 npm run dev:web`으로 지정합니다. Vite는 프론트엔드·node_modules만 파일로 제공하고 검사 산출물·환경 파일·Python 소스를 차단합니다. 이 개발 서버를 외부에 공개하지 마세요.

## 제공 화면

- 로그인과 세션 만료 처리, 대시보드·상태 필터·페이지네이션
- 프로젝트 등록·정책 수정·등록 해제, 새 작업 PRD 텍스트/파일 업로드
- 실행 전 preview·브랜치/SHA/정책 확인, 접수 응답 유실 시 동일 멱등 키 재시도
- 단계별 타임라인, 역할·레벨·검색·자동 스크롤·화면 로그 저장
- 일시중지·취소·질문 답변/한도 상향 재개·SHA 입력 후 최종 승인·명시적 상태 조정
- 테스트 시간·종료 코드·실패 상세·JUnit 다운로드·과거 회차, 리뷰 이슈 ID·해결 근거·비차단 이슈 분류
- 보존된 후보 Diff·변경 파일·바이너리/잘림 안내, 보고서의 생성 version·최신성 표시
- 수정 요청·한도 상향·승인 보류·Worktree 정리, 멱등 명령의 접수/적용 상태
- 요구사항·보고서 표시, 다크/라이트 모드·모바일·키보드 확인 대화상자

로그는 최근 최대 1,000개 구간·1 MiB를 화면에 보관하고 가상 목록으로 렌더링합니다. 화면 저장은 현재 필터·현재 보관 구간이며, 별도의 전체 저장 로그 링크는 서버에 인덱싱한 마스킹 로그를 내려받습니다. SSE는 영속 event ID로 중복·역순을 제거하고 단절 중 3초마다 이력을 조회합니다. 연결 해제는 실행 제어 요청을 보내지 않습니다.

미실행·실행 중·근거 없음·파싱 오류·시간 초과는 성공이나 0건으로 채우지 않습니다. 이전 revision의 결과는 따로 표시합니다. 이슈 해결은 같은 후보 SHA·revision의 새 테스트와 Reviewer V2의 명시적 재검증 근거가 필요합니다. 비차단 이슈의 사용자 분류는 모델의 해결과 구분하며 차단 이슈에는 허용하지 않습니다. 미등록 CLI 작업은 조회 전용입니다.

## 정적 GUI와 통합 시작

```sh
.venv/bin/python scripts/build_web.py
# 기존 API·Dispatcher·웹 서비스·Run Worker를 종료한 상태에서 실행
.venv/bin/devorchestra --config config.yaml database-upgrade
.venv/bin/devorchestra --config config.yaml web start --detach --allow-root /Users/me/projects --port 8000
.venv/bin/devorchestra --config config.yaml web-session
.venv/bin/devorchestra --config config.yaml web status
.venv/bin/devorchestra --config config.yaml web stop
```

이 방식은 `http://127.0.0.1:8000`에서 API와 GUI를 함께 제공합니다. `src/dev_orchestra/_web`는 생성물이며 Git에서 제외합니다. 빌드 후 `uv build`로 만든 wheel에는 체크섬 manifest와 GUI 파일을 포함하므로 설치 환경에 Node가 필요하지 않습니다. GUI 파일이 없는 CLI 전용 배포도 지원합니다. 임의 파일이나 API의 404를 SPA 화면으로 반환하지 않습니다.

일반 종료는 소유한 API·Dispatcher만 종료하고 실행 중 Run Worker를 유지합니다. 기존 Dispatcher를 재사용하면 종료하지 않습니다. `web stop --cancel-active`는 같은 DB의 미완료 작업 취소를 명시적으로 요청합니다. 중복 시작·포트 충돌은 기존 프로세스를 종료하지 않고 실패합니다. 업그레이드는 모든 writer를 중지한 상태에서 schema 2/3/4→5 백업·마이그레이션을 수행하며 구버전 실행 파일과 혼용하지 마세요.

## 검증 및 타입 갱신

```sh
npm run typecheck:web
npm run test:web
npm run build:web
npm run format:check --workspace apps/web
.venv/bin/python scripts/export_web_schema.py --check
npm run check:api --workspace apps/web

# API DTO를 변경한 경우
.venv/bin/python scripts/export_web_schema.py
npm run generate:api --workspace apps/web

# 실제 브라우저 + 임시 Git·SQLite·Mock Codex·실제 pytest
PLAYWRIGHT_BROWSERS_PATH=/private/tmp/devorchestra-pw-browsers npx playwright install chromium --only-shell
PLAYWRIGHT_BROWSERS_PATH=/private/tmp/devorchestra-pw-browsers npm run test:e2e --workspace apps/web
# Python이 제공하는 실제 빌드 GUI + CSP 경로로 같은 검사를 실행
E2E_BUNDLED=1 PLAYWRIGHT_BROWSERS_PATH=/private/tmp/devorchestra-pw-browsers npm run test:e2e --workspace apps/web
```

브라우저 검사는 5173/18080 포트를 사용하며 포트가 이미 사용 중이면 실패합니다. 다른 서버를 종료하거나 재사용하지 않습니다. 임시 Git 저장소를 만들고 실행 파일 Mock Codex로 별도 Worker를 구동하므로 모델 요금은 발생하지 않습니다. 상태 DB·프로젝트·정책은 사용자 기본 설정을 사용하지 않습니다. 검사 결과·스크린샷은 `test-results`, 실패 trace는 같은 폴더에 보관하며 Git에서 제외합니다. trace에는 테스트용 세션 정보가 포함될 수 있으므로 외부 공유 전 확인하세요.

현재 온라인 `npm audit`에는 개발 전용 타입 생성기의 minimatch 경로 Moderate 3건이 남아 있습니다. 배포 의존성 감사(`npm audit --omit=dev`)는 0건입니다. 생성기 입력은 저장소 코드에서 내보낸 로컬 OpenAPI로 제한하며 외부에서 받은 schema를 사용하지 않습니다. 상위 도구의 호환 패치가 나오면 갱신합니다.

Phase 5에서는 CLI 회귀·화면 단위·개발 서버 및 정적 GUI E2E와 Node 없는 wheel 실행을 검증합니다. 실제 유료 Codex V2 재검증·Linux 배포·다중 브라우저·장시간 부하는 이번 단계에서 통과로 판정하지 않습니다. Phase 6에서 서비스 수명·장애 복구·보안·패키징과 실제 Codex의 Fix/V2 재검증까지 종합 검사했습니다. macOS·단일 사용자·신뢰하는 프로젝트를 검증 범위로 유지합니다.


## Phase 6 통합·배포 검증

브라우저 검사는 기존 GUI 흐름에 더해 CLI 시작→창 닫기→동일 세션 재접속→CLI 승인, GUI 생성→CLI 승인→화면/보고서 반영을 검사합니다. 개발 서버와 Python 정적 GUI 양쪽에서 실행합니다. API·Dispatcher/Worker 강제 종료·guardian/PID 안전성과 마이그레이션 롤백·백업 복원은 별도 네이티브 Python 통합 검사로 확인합니다.

루트의 `scripts/verify_package.py`는 설치된 독립 환경에서 Node 없는 GUI·인증·정적 파일 해시·종료와 CLI 전용 설치를 확인합니다. `scripts/benchmark_logs.py`는 약 10 MiB의 한글·마스킹 로그를 제한된 페이지로 재생해 해시·커서·표본 RSS를 검사합니다. 장시간 soak나 브라우저 메모리 프로파일링을 대신하지 않습니다. 실제 모델을 사용하는 `scripts/e2e_gui.py`는 별도 명시적 승인/옵트인 검사이며 일반 테스트에서 호출하지 않습니다.

현재 지원 판정과 미검증 환경은 루트 README 및 로컬 docs/의 Phase 6 보고서를 참고하세요. docs/는 사용자의 Git 제외 정책에 따라 원격 저장소에 포함하지 않습니다.

최종 출고 판정은 **CONDITIONAL**입니다. 초기 개발/정적 GUI E2E는3개씩 통과했으나 최신 교차 E2E는2 PASS/1 FAIL(timeout)입니다. 원인 분리와 최종 전체 통과가 남아 있습니다. 테스트를 생략하거나 timeout을 늘려 통과로 처리하지 않았습니다.
