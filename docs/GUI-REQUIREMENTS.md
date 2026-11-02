# DevOrchestra — 로컬 웹 GUI 구축 요청서

## 1. 프로젝트 개요

기존에 구축한 DevOrchestra CLI 기반 멀티 에이전트 개발 자동화 시스템에 **FastAPI + React 기반 로컬 웹 GUI**를 추가하고 싶습니다.

DevOrchestra는 사용자가 기획안(PRD)을 입력하면 Codex CLI 기반 AI 에이전트들이 다음 작업을 자동 수행하는 시스템입니다.

1. Planning Agent — 기획안 분석 및 구현 계획 수립
2. Developer Agent — 별도 Feature 브랜치 생성 및 기능 개발
3. Test Agent — 테스트 코드 작성 및 기능 검증
4. Review Agent — 변경분 기반 독립 코드 리뷰
5. Fix Agent — 문제 수정
6. Test / Review Agent — 수정 결과 재검증
7. Orchestrator — 최종 결과 판단 및 사용자 승인 대기

현재 CLI에서 제공하는 기능을 웹 브라우저에서도 편리하게 사용하고, 여러 작업의 진행 상태와 에이전트 실행 로그를 실시간으로 확인할 수 있도록 구현해 주세요.

**이번 작업의 핵심은 기존 CLI MVP를 보존하면서 웹 인터페이스를 추가하는 것입니다.**

---

## 2. 개발 원칙

다음 원칙을 반드시 준수해 주세요.

- 기존 CLI 기능과 동작을 유지합니다.
- 기존 Orchestrator를 재사용합니다.
- CLI와 GUI가 별도의 개발 파이프라인 로직을 갖지 않도록 합니다.
- FastAPI는 공통 서비스 계층을 호출하는 API 서버 역할을 수행합니다.
- React는 API를 통해 상태를 조회하고 사용자 명령을 전달합니다.
- Codex CLI 실행은 백엔드에서만 수행합니다.
- 실행 상태와 작업 이력은 SQLite에 저장합니다.
- 개발 작업은 기존 Git Worktree 격리 정책을 유지합니다.
- 브라우저를 닫거나 새로고침해도 백엔드 작업은 계속 실행되어야 합니다.
- 사용자 승인 없이 Git Push 또는 Merge를 수행하지 않습니다.
- 우선 단일 사용자 로컬 환경을 대상으로 구현합니다.
- 향후 다중 사용자 및 원격 배포를 고려하되, 현재 범위에서 과도한 복잡성을 도입하지 않습니다.

기존 프로젝트의 실제 코드와 구조를 먼저 분석하고, 현재 구현된 인터페이스를 최대한 활용해 주세요.

요청서에 제시된 경로, API, 데이터 모델은 권장안입니다. 기존 구조와 충돌한다면 호환성을 유지하는 방향으로 조정해 주세요.

---

## 3. 기술 스택

### Backend

- Python 3.11 이상
- FastAPI
- Uvicorn
- Pydantic
- SQLite
- 기존 Orchestrator 및 Agent 모듈
- Codex CLI
- Git CLI 및 Git Worktree
- Server-Sent Events(SSE) 기반 실시간 이벤트 스트리밍

### Frontend

- React
- TypeScript
- Vite
- Tailwind CSS
- React Router
- TanStack Query
- Zustand (필요한 경우에만)
- Lucide React

UI 컴포넌트는 shadcn/ui를 우선 검토해 주세요.

서버 상태는 TanStack Query를 중심으로 관리하고, Zustand는 실제로 필요한 클라이언트 전역 상태에만 사용해 주세요.

### 기타

- npm 또는 pnpm
- pytest
- Vitest
- React Testing Library
- Playwright (핵심 E2E 테스트)

---

## 4. 시스템 아키텍처

다음 구조를 기본으로 설계해 주세요.

```text
                 ┌───────────────────────┐
                 │      React GUI        │
                 │   localhost:5173      │
                 └───────────┬───────────┘
                             │
                       HTTP / SSE
                             │
                 ┌───────────▼───────────┐
                 │      FastAPI          │
                 │   localhost:8000      │
                 └───────────┬───────────┘
                             │
                 ┌───────────▼───────────┐
                 │   Application Layer   │
                 │                       │
                 │  RunService           │
                 │  ProjectService       │
                 │  ApprovalService      │
                 │  EventService         │
                 └───────────┬───────────┘
                             │
                 ┌───────────▼───────────┐
                 │     Orchestrator      │
                 │                       │
                 │  Planning Agent       │
                 │  Developer Agent      │
                 │  Test Agent           │
                 │  Review Agent         │
                 │  Fix Agent            │
                 └───────────┬───────────┘
                             │
              ┌──────────────┼──────────────┐
              │              │              │
        ┌─────▼─────┐  ┌─────▼─────┐  ┌─────▼─────┐
        │ Codex CLI │  │ SQLite    │  │ Git       │
        │           │  │           │  │ Worktree  │
        └───────────┘  └───────────┘  └───────────┘
```

CLI도 동일한 Application Layer를 호출하도록 유지해 주세요.

중요한 설계 조건:

1. React에서 Codex CLI를 직접 실행하지 않습니다.
2. FastAPI 엔드포인트에서 장시간 Codex 작업을 동기적으로 실행하지 않습니다.
3. 백그라운드 작업 실행기는 Orchestrator와 분리합니다.
4. 실행 상태는 메모리에만 보관하지 않고 SQLite에 영속화합니다.
5. SSE는 상태와 로그를 전달하는 용도로 사용하며, 명령 실행은 HTTP API를 사용합니다.
6. 프론트엔드에서 발생한 작업 중단, 승인, 재시도 요청은 서버 측 상태 검증을 거쳐 처리합니다.

---

## 5. 구현할 주요 화면

### 5.1 Dashboard

DevOrchestra에 등록된 프로젝트와 전체 개발 작업 상태를 한눈에 확인할 수 있는 메인 화면을 구현해 주세요.

표시 항목:

- 등록된 프로젝트 수
- 실행 중인 작업 수
- 승인 대기 작업 수
- 실패한 작업 수
- 최근 실행 목록
- 최근 완료된 작업
- 에이전트별 실행 현황

작업 상태는 다음과 같이 구분합니다.

- Queued
- Planning
- Developing
- Testing
- Reviewing
- Fixing
- Waiting Approval
- Completed
- Failed
- Cancelled

각 작업을 클릭하면 상세 화면으로 이동해야 합니다.

### 5.2 Projects — 프로젝트 관리

사용자가 로컬 Git 프로젝트를 등록하고 관리할 수 있어야 합니다.

필수 기능:

- 프로젝트 등록
- 프로젝트 목록 조회
- 프로젝트 상세 조회
- 프로젝트 설정 수정
- 프로젝트 등록 해제
- Git 저장소 유효성 검사
- 로컬 브랜치 목록 조회
- 기준 브랜치 선택
- 프로젝트별 테스트 명령 설정
- 프로젝트별 리뷰 정책 설정

프로젝트 등록 시 입력 항목:

- 프로젝트 이름
- 로컬 프로젝트 경로
- 기본 기준 브랜치
- 테스트 명령
- 최대 자동 수정 횟수
- 리뷰 차단 심각도

예시:

```json
{
  "name": "my-backend",
  "repository_path": "/Users/developer/projects/my-backend",
  "default_base_branch": "develop",
  "test_commands": [
    "./gradlew test"
  ],
  "max_fix_attempts": 3,
  "blocking_severities": [
    "critical",
    "high"
  ]
}
```

로컬 경로는 사용자가 명시적으로 등록한 경로만 허용하고, 임의 경로 탐색이나 경로 순회 공격을 방지해 주세요.

브라우저에서 로컬 디렉터리 선택 기능이 제한되는 경우, 경로 직접 입력 및 유효성 검사를 제공해 주세요.

### 5.3 New Run — 개발 작업 생성

사용자가 새로운 자동 개발 작업을 생성하는 화면입니다.

입력 항목:

- 프로젝트 선택
- PRD 파일 업로드 또는 텍스트 직접 입력
- 기준 브랜치 선택
- Feature 브랜치 이름 (선택)
- 최대 수정 반복 횟수
- 테스트 실행 여부
- 코드 리뷰 실행 여부
- 추가 지시사항

작업 시작 전에 다음 정보를 확인할 수 있도록 해 주세요.

- 대상 프로젝트
- 기준 브랜치
- 생성할 Feature 브랜치
- PRD 내용 요약
- 적용될 개발 및 리뷰 정책

작업 생성 시 서버는 실행 ID를 발급하고, 작업을 큐에 등록한 뒤 즉시 응답해야 합니다.

예시:

```json
{
  "run_id": "run-20261102-001",
  "status": "queued"
}
```

### 5.4 Run Detail — 실행 상세 화면

가장 중요한 화면입니다.

한 작업의 전체 개발 프로세스를 실시간으로 확인할 수 있어야 합니다.

상단 정보:

- 작업 ID
- 프로젝트 이름
- Feature 브랜치
- 기준 브랜치
- 현재 상태
- 시작 시간
- 경과 시간
- 현재 수정 반복 횟수
- 최종 커밋 SHA

중앙에는 다음 단계가 표시되는 워크플로 타임라인을 구현해 주세요.

```text
Planning     ✓ Completed
    │
Developer    ✓ Completed
    │
Testing      ✓ Passed
    │
Review       ! Issues Found
    │
Fixing       ✓ Completed
    │
Testing      ✓ Passed
    │
Review       ● Running
    │
Approval     ○ Waiting
```

각 단계를 클릭하면 해당 에이전트의 실행 결과와 로그를 확인할 수 있어야 합니다.

반복된 Test / Review / Fix 단계는 덮어쓰지 말고 반복 회차별로 구분해서 보여주세요.

예:

- Iteration 1 — 테스트 실패
- Iteration 2 — 테스트 통과, 리뷰 문제 발견
- Iteration 3 — 테스트 및 리뷰 통과

### 5.5 Agent Logs — 실시간 로그

각 에이전트의 실행 로그를 실시간으로 표시해 주세요.

필수 기능:

- 전체 로그 보기
- 에이전트별 필터
- 로그 레벨별 필터
- 자동 스크롤
- 자동 스크롤 일시정지
- 키워드 검색
- 로그 다운로드
- 실행 단계별 로그 구분

SSE를 통해 로그를 스트리밍해 주세요.

로그 스트림이 끊어지면 자동 재연결해야 하며, 마지막 이벤트 ID를 기준으로 누락된 이벤트를 다시 받을 수 있어야 합니다.

서버 재시작 후에도 기존 로그를 조회할 수 있어야 합니다.

인증 토큰, API 키, 환경변수의 민감한 값은 로그에 출력하지 않아야 합니다.

### 5.6 Test Results — 테스트 결과

Test Agent의 검증 결과를 보여주는 화면입니다.

표시 항목:

- 전체 테스트 수
- 성공 수
- 실패 수
- 건너뛴 테스트 수
- 테스트 실행 시간
- 테스트 명령 및 종료 코드
- 실패한 테스트 목록
- 실패 메시지
- 테스트 파일 및 관련 코드 위치

테스트 결과가 없는 경우 다음 상태를 구분해야 합니다.

- 아직 실행되지 않음
- 실행 중
- 실행 성공
- 실행 실패
- 실행 불가
- 결과 파싱 실패

실행되지 않은 테스트를 성공으로 표시해서는 안 됩니다.

### 5.7 Code Review — 코드 리뷰 결과

Review Agent가 발견한 문제를 시각적으로 확인할 수 있어야 합니다.

심각도:

- Critical
- High
- Medium
- Low

각 리뷰 항목에 다음 정보를 표시해 주세요.

- 이슈 ID
- 심각도
- 파일 경로
- 라인 번호
- 문제 설명
- 발생 가능성 및 근거
- 수정 권장 사항
- 발견된 반복 회차
- 현재 해결 상태

해결 상태:

- Open
- Fixed
- Unresolved
- Dismissed

이슈를 클릭하면 해당 코드의 변경분을 확인할 수 있도록 구현해 주세요.

가능하다면 Monaco Editor 또는 적절한 Diff Viewer를 사용해 다음 정보를 표시해 주세요.

- 기존 코드
- 변경된 코드
- 변경된 라인
- 리뷰 지적 위치

파일 경로와 라인 번호가 현재 커밋에서도 유효한지 확인하고, 변경으로 인해 위치가 달라졌다면 이를 명확하게 표시해 주세요.

리뷰 이슈를 자동으로 해결된 것으로 판단할 때는 단순히 다음 리뷰 결과에서 사라졌다는 이유만으로 확정하지 말고, 이슈 식별 및 검증 기준을 마련해 주세요.

### 5.8 Final Report — 최종 보고서

모든 검증이 끝나면 최종 보고서를 표시해 주세요.

보고서 내용:

- 작업 개요
- PRD 요약
- 구현 기능
- 변경 파일 목록
- Git 커밋 목록
- 테스트 결과
- 코드 리뷰 결과
- 자동 수정 이력
- 해결되지 않은 문제
- 최종 검증 상태
- 승인 필요 여부

Markdown 형태로 보고서를 표시하고 다운로드할 수 있어야 합니다.

### 5.9 Approval — 사용자 승인

검증이 완료된 작업은 Waiting Approval 상태가 됩니다.

사용자는 다음 작업을 수행할 수 있어야 합니다.

- 최종 결과 승인
- 승인 보류
- 수정 요청
- 작업 취소

수정 요청 시 사용자가 추가 지시사항을 입력하면 Fix Agent가 이를 처리하도록 합니다.

수정 후에는 테스트 및 코드 리뷰를 다시 수행해야 합니다.

승인 버튼은 내부 실행 상태를 변경하는 기능이며, Git Push 또는 Merge를 자동 수행하지 않습니다.

승인 당시 검증한 최종 커밋 SHA를 기록하고, 이후 코드가 변경되면 기존 승인이 무효화되도록 해 주세요.

---

## 6. FastAPI 설계

기존 CLI와 동일한 서비스 계층을 사용하는 REST API를 구현해 주세요.

### Projects API

| Method | Endpoint | 기능 |
|---|---|---|
| GET | `/api/projects` | 프로젝트 목록 |
| POST | `/api/projects` | 프로젝트 등록 |
| GET | `/api/projects/{id}` | 프로젝트 상세 |
| PATCH | `/api/projects/{id}` | 프로젝트 수정 |
| DELETE | `/api/projects/{id}` | 프로젝트 등록 해제 |
| GET | `/api/projects/{id}/branches` | 브랜치 목록 |

### Runs API

| Method | Endpoint | 기능 |
|---|---|---|
| GET | `/api/runs` | 실행 목록 |
| POST | `/api/runs` | 새 작업 생성 |
| GET | `/api/runs/{id}` | 실행 상세 |
| POST | `/api/runs/{id}/cancel` | 실행 취소 |
| POST | `/api/runs/{id}/resume` | 실행 재개 |
| POST | `/api/runs/{id}/approve` | 최종 승인 |
| POST | `/api/runs/{id}/request-changes` | 수정 요청 |
| GET | `/api/runs/{id}/report` | 최종 보고서 |

### Results API

| Method | Endpoint | 기능 |
|---|---|---|
| GET | `/api/runs/{id}/steps` | 단계별 실행 결과 |
| GET | `/api/runs/{id}/tests` | 테스트 결과 |
| GET | `/api/runs/{id}/reviews` | 리뷰 결과 |
| GET | `/api/runs/{id}/changes` | 변경 파일 목록 |
| GET | `/api/runs/{id}/diff` | Git Diff |
| GET | `/api/runs/{id}/logs` | 저장된 로그 |
| GET | `/api/runs/{id}/events` | SSE 이벤트 스트림 |

API 요청 및 응답은 Pydantic 모델로 정의하고, FastAPI OpenAPI 문서를 제공해 주세요.

실행 ID와 프로젝트 ID는 안전하게 검증하고, 잘못된 상태 전이에 대해 적절한 HTTP 오류를 반환해야 합니다.

목록 API에는 페이지네이션과 기본 필터링을 지원해 주세요.

---

## 7. 백그라운드 작업 실행 설계

Codex 실행은 수 분에서 수십 분 이상 걸릴 수 있습니다.

따라서 FastAPI 요청 처리 프로세스에서 직접 장시간 작업을 실행하지 않도록 해 주세요.

다음 구조를 권장합니다.

```text
React
  │
  ▼
POST /api/runs
  │
  ▼
FastAPI
  │
  ├── 실행 정보 SQLite 저장
  └── 작업 큐 등록
          │
          ▼
     Worker Process
          │
          ▼
     Orchestrator
          │
          ▼
     Codex Agents
```

MVP에서는 SQLite 기반 작업 큐 또는 별도 로컬 Worker 프로세스를 사용해도 됩니다.

단, 다음 조건을 만족해야 합니다.

1. 브라우저를 닫아도 작업이 유지되어야 합니다.
2. API 서버가 재시작되어도 작업 상태가 보존되어야 합니다.
3. Worker 비정상 종료를 감지할 수 있어야 합니다.
4. 동일 작업이 중복 실행되지 않아야 합니다.
5. 재시작 시 중단된 작업을 안전하게 복구할 수 있어야 합니다.
6. 실행 중인 Codex 프로세스를 취소할 수 있어야 합니다.
7. 기본적으로 동시 실행 수를 제한해야 합니다.
8. 같은 프로젝트의 충돌 가능성이 있는 작업을 적절히 직렬화해야 합니다.

FastAPI의 단순 `BackgroundTasks`만으로 프로세스 재시작 및 작업 복구를 해결했다고 간주하지 마세요.

작업 취소 시에는 하위 Codex 프로세스까지 정상적으로 종료하고, 필요한 경우 타임아웃 후 강제 종료할 수 있도록 해 주세요.

---

## 8. 데이터 모델 및 SQLite 설계

기존 SQLite 구조를 확인한 뒤 필요한 테이블과 컬럼을 추가해 주세요.

권장 엔터티:

### Project

- id
- name
- repository_path
- default_base_branch
- test_commands
- review_policy
- created_at
- updated_at

### Run

- id
- project_id
- spec_path 또는 저장된 PRD 참조
- base_branch
- feature_branch
- base_commit_sha
- current_commit_sha
- status
- current_agent
- current_iteration
- max_fix_attempts
- created_at
- started_at
- completed_at

### RunStep

- id
- run_id
- agent_type
- iteration
- status
- started_at
- completed_at
- exit_code
- result_path
- error_message

### ReviewIssue

- id
- run_id
- iteration
- severity
- file_path
- line_number
- description
- suggestion
- status
- created_at
- resolved_at

### RunEvent

- id
- run_id
- step_id
- event_type
- message
- timestamp

### Approval

- id
- run_id
- approved_commit_sha
- status
- comment
- created_at

실제 데이터베이스 구조는 정규화와 기존 스키마 호환성을 고려하여 결정해 주세요.

데이터베이스 마이그레이션 방식을 마련하고, 기존 CLI 실행 이력을 손실하지 않도록 해 주세요.

---

## 9. 프론트엔드 구조

다음과 같은 디렉터리 구조를 권장합니다.

```text
frontend/
├── package.json
├── vite.config.ts
├── tsconfig.json
├── index.html
│
├── src/
│   ├── main.tsx
│   ├── App.tsx
│   │
│   ├── api/
│   │   ├── client.ts
│   │   ├── projects.ts
│   │   ├── runs.ts
│   │   └── events.ts
│   │
│   ├── components/
│   │   ├── layout/
│   │   ├── workflow/
│   │   ├── logs/
│   │   ├── review/
│   │   ├── tests/
│   │   └── common/
│   │
│   ├── pages/
│   │   ├── DashboardPage.tsx
│   │   ├── ProjectsPage.tsx
│   │   ├── ProjectDetailPage.tsx
│   │   ├── NewRunPage.tsx
│   │   ├── RunDetailPage.tsx
│   │   └── SettingsPage.tsx
│   │
│   ├── hooks/
│   ├── types/
│   ├── utils/
│   └── styles/
│
└── tests/
```

컴포넌트는 재사용 가능한 단위로 분리하고, API 호출 로직과 화면 렌더링 로직을 명확하게 구분해 주세요.

---

## 10. UI/UX 디자인 요구사항

개발자가 매일 사용하는 도구이므로 복잡한 장식보다 정보의 명확성과 조작 편의성을 우선해 주세요.

### 디자인 방향

- GitHub, Linear, Vercel Dashboard와 유사한 개발자 도구 스타일
- 깔끔하고 현대적인 UI
- 기본 다크 모드
- 라이트 모드 지원
- 좌측 사이드바 내비게이션
- 상단 작업 상태 표시
- 반응형 레이아웃
- 긴 로그 및 Diff를 읽기 편한 구성

### 색상 및 상태 표현

- 성공: Green
- 진행 중: Blue
- 경고: Amber
- 실패: Red
- 대기: Gray

색상만으로 상태를 구분하지 말고 아이콘과 텍스트를 함께 사용해 주세요.

### 주요 UX 요구사항

- 새로고침 후에도 현재 작업 상태 유지
- 네트워크 오류 시 명확한 오류 메시지 표시
- SSE 연결 끊김 시 자동 재연결
- 작업 취소 시 확인 대화상자 표시
- 최종 승인 시 커밋 SHA 및 결과 재확인
- 실행 중 작업과 완료된 작업 명확히 구분
- 로딩, 빈 화면, 실패 상태 처리
- 키보드 접근성 고려

---

## 11. 보안 요구사항

로컬 전용 도구이지만 Codex를 통해 실제 파일 수정과 명령 실행이 가능하므로 보안을 중요하게 고려해야 합니다.

필수 요구사항:

1. 기본적으로 `127.0.0.1`에만 서버 바인딩
2. 외부 네트워크에 API 서버 공개 금지
3. 등록된 프로젝트 경로만 접근 허용
4. 프로젝트 경로 및 심볼릭 링크 검증
5. Shell 명령 실행 시 문자열 결합 방식 지양
6. subprocess 인자를 배열 형태로 전달
7. 사용자 입력값을 Shell 명령에 직접 삽입하지 않기
8. Codex 실행 권한은 기존 Agent별 정책 유지
9. Git Worktree 격리 정책 유지
10. 로그에서 비밀정보 마스킹
11. PRD 및 저장소 파일의 프롬프트 인젝션 방어
12. API 입력값 검증
13. CORS 허용 Origin 제한
14. 로컬 API에 대한 임의 웹사이트의 요청 방어
15. 위험한 작업에 대한 사용자 확인
16. 승인 없이 원격 Push 또는 Merge 금지

특히 localhost 서비스라도 브라우저를 통한 CSRF 및 악의적인 외부 사이트의 요청 가능성을 고려해 주세요.

Origin 검증, 적절한 인증 토큰 또는 세션 보호, CSRF 방어 중 현재 구조에 적합한 방식을 선택하고 근거를 설명해 주세요.

CORS 설정만으로 API 접근이 보호된다고 간주하지 마세요.

---

## 12. CLI와 GUI의 호환성

다음 시나리오를 반드시 지원해야 합니다.

**시나리오 A — CLI에서 시작한 작업을 GUI에서 확인**

```bash
devorchestra run \
  --repo ~/projects/my-project \
  --spec PRD.md \
  --base develop
```

이 명령으로 생성된 작업이 GUI Dashboard와 Run Detail 화면에 나타나야 합니다.

**시나리오 B — GUI에서 시작한 작업을 CLI에서 확인**

GUI에서 새 작업을 생성한 뒤 다음 명령으로 조회할 수 있어야 합니다.

```bash
devorchestra list
devorchestra status <run-id>
devorchestra logs <run-id>
```

**시나리오 C — GUI에서 승인 대기 중인 작업을 CLI로 승인**

```bash
devorchestra approve <run-id>
```

CLI에서 승인하면 GUI에도 상태 변경이 반영되어야 합니다.

**시나리오 D — 실행 중 서버 재시작**

API 서버를 재시작해도 Worker가 독립적으로 실행 중이면 작업이 유지되어야 합니다.

Worker까지 종료된 경우에는 저장된 상태를 기반으로 안전하게 재개할 수 있어야 합니다.

CLI와 GUI는 동일한 SQLite 데이터베이스와 서비스 계층을 사용해야 하며, 동시 접근 시 데이터 정합성이 유지되어야 합니다.

---

## 13. 개발 단계

다음 순서로 구현해 주세요.

### Phase 1 — 기존 프로젝트 분석

- 현재 CLI 구조 분석
- Orchestrator 인터페이스 확인
- SQLite 스키마 확인
- Codex 실행 방식 확인
- 기존 테스트 실행
- GUI 연동을 위해 필요한 변경 사항 정리

기존 구조를 확인하지 않고 대규모 리팩터링을 시작하지 마세요.

### Phase 2 — 공통 서비스 계층

- ProjectService
- RunService
- ApprovalService
- EventService
- CLI와 서비스 계층 연결
- 기존 CLI 회귀 테스트

### Phase 3 — FastAPI

- 프로젝트 관리 API
- 실행 관리 API
- 결과 조회 API
- 승인 API
- SSE 이벤트 스트림
- OpenAPI 문서

### Phase 4 — Worker 및 이벤트 처리

- 백그라운드 작업 큐
- Worker 실행
- 상태 저장
- 로그 이벤트 생성
- 중단 및 재개
- 중복 실행 방지

### Phase 5 — React GUI

- 공통 레이아웃
- Dashboard
- Projects
- New Run
- Run Detail
- Agent Logs
- Test Results
- Code Review
- Final Report
- Approval

### Phase 6 — 통합 테스트

- API 테스트
- 프론트엔드 컴포넌트 테스트
- CLI/GUI 상태 공유 테스트
- SSE 재연결 테스트
- Worker 재시작 테스트
- 작업 취소 테스트
- 승인 상태 검증 테스트
- 실제 브라우저 E2E 테스트

---

## 14. 로컬 실행 환경

개발 환경에서는 다음과 같이 실행할 수 있도록 해 주세요.

### Backend

```bash
uvicorn dev_orchestra.api.main:app \
  --host 127.0.0.1 \
  --port 8000 \
  --reload
```

### Worker

```bash
devorchestra worker start
```

### Frontend

```bash
cd frontend
npm install
npm run dev
```

기본 접속 주소:

```text
Frontend: http://localhost:5173
Backend:  http://127.0.0.1:8000
API Docs: http://127.0.0.1:8000/docs
```

가능하면 개발자가 한 번의 명령으로 로컬 서비스를 시작할 수 있도록 실행 스크립트를 제공해 주세요.

예시:

```bash
devorchestra web start
```

이 명령은 API 서버, Worker, 프론트엔드를 안전하게 시작하고 종료할 수 있어야 합니다.

운영 모드에서는 프론트엔드를 빌드하여 FastAPI에서 정적 파일로 제공하는 방식도 지원할 수 있도록 설계해 주세요.

---

## 15. 테스트 및 완료 조건

다음 조건을 모두 만족해야 GUI MVP 구현이 완료된 것으로 판단합니다.

- 기존 CLI 명령 정상 동작
- 로컬 Git 프로젝트 등록 가능
- PRD 입력 및 개발 작업 생성 가능
- Feature 브랜치 및 Worktree 자동 생성
- Codex 에이전트 실행 가능
- Dashboard에서 작업 상태 확인 가능
- Run Detail에서 단계별 진행 상황 확인 가능
- 에이전트 로그 실시간 표시
- 테스트 결과 확인 가능
- 코드 리뷰 이슈 확인 가능
- Git Diff 확인 가능
- 수정 반복 이력 확인 가능
- 실행 중 작업 취소 가능
- 실패한 작업 재개 가능
- 최종 보고서 확인 및 다운로드 가능
- GUI에서 승인 및 수정 요청 가능
- CLI와 GUI의 작업 상태 동기화
- 브라우저 종료 후에도 작업 지속
- API 서버 재시작 후 상태 복구
- 기존 프로젝트 작업 트리 보호
- 사용자 승인 없이 Push 및 Merge하지 않음

실제 Codex 호출이 필요한 테스트와 Mock 기반 테스트를 구분하고, 실행하지 못한 검증은 명확히 보고해 주세요.

---

## 16. 최종 구현 지시

위 요구사항을 바탕으로 기존 DevOrchestra CLI 프로젝트에 FastAPI + React 웹 GUI를 실제로 구현해 주세요.

다음 사항을 반드시 지켜 주세요.

1. 기존 코드베이스를 먼저 분석해 주세요.
2. 현재 CLI 기능을 손상시키지 마세요.
3. CLI와 GUI의 비즈니스 로직을 중복 구현하지 마세요.
4. 실제 실행 가능한 코드를 작성해 주세요.
5. 기능별로 테스트를 작성하고 실행해 주세요.
6. Mock 데이터로만 동작하는 GUI를 완성본으로 간주하지 마세요.
7. 백엔드 API와 프론트엔드를 실제로 연결해 주세요.
8. 장시간 Codex 실행을 안정적으로 처리해 주세요.
9. 작업 중단, 실패, 복구 상황을 반드시 고려해 주세요.
10. 불필요하게 복잡한 프레임워크나 인프라를 도입하지 마세요.
11. 설치 및 실행 방법을 README에 상세히 작성해 주세요.
12. 구현 완료 후 실제 동작 확인 결과를 보고해 주세요.
13. 구현하지 못한 기능이나 알려진 제약 사항을 명확히 표시해 주세요.

**최종 목표는 사용자가 로컬 브라우저에서 DevOrchestra를 열고, 프로젝트 선택 → PRD 입력 → 자동 개발 실행 → 실시간 진행 상황 확인 → 테스트 및 리뷰 결과 확인 → 최종 승인까지 수행할 수 있는 웹 애플리케이션을 완성하는 것입니다.**

우선 기존 CLI와 호환되는 안정적인 GUI MVP를 완성하고, 다중 사용자 인증, 클라우드 배포, GitHub/GitLab 자동 PR 생성 등은 향후 확장 과제로 남겨 주세요.