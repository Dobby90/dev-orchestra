# DevOrchestra 웹 화면 — Phase 4

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
- 요구사항·기존 보고서 표시, 다크/라이트 모드·모바일·키보드 확인 대화상자

로그는 최근 최대 1,000개 구간·1 MiB를 화면에 보관하고 가상 목록으로 렌더링합니다. 다운로드는 현재 필터·현재 보관 구간이며 전체 서버 로그 다운로드와 구분합니다. SSE는 영속 event ID로 중복·역순을 제거하고 단절 중 3초마다 이력을 조회합니다. 연결 해제는 실행 제어 요청을 보내지 않습니다.

현재 백엔드에 없는 JUnit 상세·리뷰 이슈 수명·Diff·수정 요청 API는 후속 통합 범위입니다. 해당 결과 탭은 사용 불가를 표시하며 테스트 성공·이슈 0건으로 가장하지 않습니다. 미등록 CLI 작업은 조회 전용입니다. 정적 파일 Python 패키징과 통합 `web start`는 Phase 5에서 연결합니다.

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
```

브라우저 검사는 5173/18080 포트를 사용하며 포트가 이미 사용 중이면 실패합니다. 다른 서버를 종료하거나 재사용하지 않습니다. 임시 Git 저장소를 만들고 실행 파일 Mock Codex로 별도 Worker를 구동하므로 모델 요금은 발생하지 않습니다. 상태 DB·프로젝트·정책은 사용자 기본 설정을 사용하지 않습니다. 검사 결과·스크린샷은 `test-results`, 실패 trace는 같은 폴더에 보관하며 Git에서 제외합니다. trace에는 테스트용 세션 정보가 포함될 수 있으므로 외부 공유 전 확인하세요.

현재 온라인 `npm audit`에는 개발 전용 타입 생성기의 minimatch 경로 Moderate 3건이 남아 있습니다. 배포 의존성 감사(`npm audit --omit=dev`)는 0건입니다. 생성기 입력은 저장소 코드에서 내보낸 로컬 OpenAPI로 제한하며 외부에서 받은 schema를 사용하지 않습니다. 상위 도구의 호환 패치가 나오면 갱신합니다.
