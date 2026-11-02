> 후속 수정: 이 문서는 `5f93bde` 대상의 수정 전 감사 기록입니다. `99bb906`에서 수행한 결함 수정과 재검증은 [수정 및 검증 결과](cli-mvp-remediation-results.md)를 참고하세요. 아래 FAIL·NOT_READY 집계는 과거 평가를 보존한 것입니다.

# CLI MVP GUI 착수 준비도 검증 보고서

검증일: 2026-11-02 (Asia/Seoul)  
대상 커밋: `5f93bde38eda2279c7c51d486cc5a47d65598bfa`  
최종 판정: **NOT_READY**

## 판정 근거

정상 PRD 흐름과 기본 Worktree 격리·수정 반복·일반 취소는 재현에 성공했습니다. 그러나 검증된 SHA와 다른 코드를 승인할 수 있고, 하위 프로세스 종료·테스트 보호·로그 비밀값 처리·호스트 Git 실행 경계·리뷰 프로토콜에 차단 결함이 존재합니다. GUI에서 실행·승인 버튼을 제공하기 전에 해당 경계를 보완해야 합니다.

기존 테스트 38개가 모두 통과한다는 사실은 확인했습니다. 추가 검증 항목에서는 실패를 별도로 재현했으므로 기존 테스트 통과를 시스템 전체의 안전성 통과로 해석하지 않습니다. 이전 `docs/VALIDATION.md`는 과거 기록으로 유지하고 이번 보고서가 현 커밋의 GUI 준비도 평가를 제공합니다.

## 집계

| 지표 | 결과 |
|---|---:|
| 전체 품질 검증 항목 | 48 |
| PASS | 26 |
| FAIL | 16 |
| NOT_TESTED | 5 |
| NOT_APPLICABLE | 1 |
| 필수 항목 | 41 |
| 필수 PASS | 24 |
| 필수 FAIL | 14 |
| 필수 NOT_TESTED | 3 |
| 필수 통과율 | **24/41 = 58.5%** |
| Critical 결함 | 0건 |
| High 결함 | 7건 |
| Medium 결함 | 8건 |

통과율의 분모에는 필수 NOT_TESTED도 포함했습니다. NOT_APPLICABLE은 제외합니다. 위 48개는 고유 ID를 가진 품질 항목 수이며 pytest 테스트 38개와 다른 집계입니다. 한 결함이 여러 품질 항목에 영향을 줄 수 있어 FAIL 16건과 결함 15건도 다릅니다.

## 수행 환경과 검증 한계

- Python 3.12.16, macOS, 설치된 Codex CLI **0.166.0**.
- 일반 기존 테스트: 37개 통과, OS 샌드박스 선택 실행 1개 제외.
- 실제 OS 샌드박스를 포함한 기존 테스트: 38개 통과.
- 추가 검증은 임시 저장소·로컬 bare 원격·실행 파일 형태 Mock Codex·실제 pytest와 OS 샌드박스로 수행했습니다.
- 실제 Codex 모델 호출은 **0회**입니다. `codex login status`의 `Logged in using ChatGPT`는 저장된 로그인 상태이며 서버 인증 성공·남은 한도·청구 조건 검증이 아닙니다. 0.165.0에서의 과거 실제 E2E는 0.166.0의 PASS 근거로 재사용하지 않았습니다.
- 실제 사용자 프로젝트·GitHub 원격을 변경하지 않았습니다. 새 파일은 요청된 보고서 4개뿐이며 기존 구현·테스트·스크립트는 수정하지 않았습니다.

## Critical 및 High 결함

Critical은 이번 범위에서 입증된 것이 없습니다. 다음 High 7건은 재현됐습니다.

| ID | 결함 | 근거 항목 |
|---|---|---|
| Q01 | 검증 SHA와 승인 SHA의 연결 누락 및 정리 후 유효성 확인 누락 | C01, F04 |
| Q02 | 시간 초과 후 SIGTERM을 무시하는 자식이 살아남음 | B04 |
| Q03 | 호스트 Git clean filter가 샌드박스 밖에서 실행됨 | C02 |
| Q04 | Test Agent가 기존 실패 테스트를 무력화할 수 있음 | D01 |
| Q05 | 일반 JSON token·Bearer 값이 로그에 그대로 남음 | B03 |
| Q06 | Codex 사전 기능 검사가 타임아웃·취소 제어 밖에 있음 | B02 |
| Q07 | 불완전한 리뷰 JSON을 문제 없음으로 해석 | G01 |

C01에서는 실제 pytest가 통과한 후 reporting 구간에 외부 커밋을 넣었습니다. 테스트 SHA와 최종 SHA가 달랐는데도 승인이 `completed`로 기록됐고, 승인된 코드를 실제로 재테스트하면 실패했습니다. 외부 변경은 재현용 Runner의 report 진입점에서 주입했으며 승인·보고서 구현 자체는 교체하지 않았습니다.

C02는 저장소에 실행 가능한 clean filter driver가 설정된 조건에서 재현했습니다. 단순 PRD 문자열만으로 임의의 호스트 명령을 실행할 수 있다고 일반화하지 않습니다. 상세 조건과 실제 관찰값은 [테스트 결과](cli-mvp-test-results.md)와 [알려진 문제](cli-mvp-known-issues.md)를 참고하세요.

## GUI 개발 전 반드시 수정할 문제

1. Q01~Q07: 승인 증거 SHA 연결, 하위 프로세스 종료, 호스트 Git 신뢰 경계, 테스트 무력화 방지, 로그 보호, 사전 호출 시간 제한, 역할별 필수 JSON 검증.
2. Q08~Q09: 취소 가능한 큐/잠금 대기, 합법적 상태 전이·조건부 갱신, DB 버전과 마이그레이션 정책.
3. Q10~Q12, Q15: 테스트 보고서의 전역 오류 검증, 상태와 보고서 일치, 입력 오류 정규화, 재개 전 선행 증거 검증.
4. 수정 후 N01·N02·N04를 실제 모델·권한·장애 경계 검증으로 보완하고 필수 FAIL/NOT_TESTED를 해소해야 READY 판정을 재검토할 수 있습니다.

## GUI와 병행 가능한 개선

Q13의 실패 보고서 공통화, Q14의 이벤트·로그 커서 계약, 조회 페이지네이션, 버전 있는 API DTO 설계, 대시보드의 빈 화면·로딩·읽기 전용 결과 표시, Linux/다른 도구 체인 검증 계획은 병행할 수 있습니다. 실제 실행·취소·승인 API 연결은 차단 결함 해소 이후로 두는 것을 권장합니다.

공통 RunService와 별도 Worker 진입점은 존재하며 직접 호출·직렬화가 동작했습니다. 그러나 이를 그대로 FastAPI에서 노출하면 동시성·취소·무결성 문제까지 함께 노출되므로 구조 분리의 PASS가 운영 준비 완료를 뜻하지 않습니다. 구체적 순서는 [GUI 준비 권고](gui-readiness-recommendations.md)에 정리했습니다.

## 전체 항목

| ID | 검증 항목 | 중요도 | 상태 | 결함 |
|---|---|---|---|---|
| [A01](cli-mvp-test-results.md#a01) | CLI 기본 실행·조회·승인 및 알 수 없는 실행 ID 오류 | 필수 | PASS | — |
| [A02](cli-mvp-test-results.md#a02) | PRD→계획→개발→테스트 생성·실행→리뷰→승인 | 필수 | PASS | — |
| [A03](cli-mvp-test-results.md#a03) | 실제 테스트 실패→Fix→테스트·리뷰 재실행 | 필수 | PASS | — |
| [A04](cli-mvp-test-results.md#a04) | 자동 수정 한도 0회 | 필수 | PASS | — |
| [A05](cli-mvp-test-results.md#a05) | Critical/High 차단 및 Medium/Low 기본 비차단 | 필수 | PASS | — |
| [B01](cli-mvp-test-results.md#b01) | 잘못된 JSON·인증 실패·실행 타임아웃 처리 | 필수 | PASS | — |
| [B02](cli-mvp-test-results.md#b02) | Codex 사전 기능 확인의 시간 제한·취소 가능성 | 필수 | FAIL | Q06 |
| [B03](cli-mvp-test-results.md#b03) | JSON·Bearer 형식 로그의 민감값 마스킹 | 필수 | FAIL | Q05 |
| [B04](cli-mvp-test-results.md#b04) | 타임아웃 후 자식 프로세스 전체 종료 | 필수 | FAIL | Q02 |
| [C01](cli-mvp-test-results.md#c01) | 최종 승인 대상과 실제 테스트·리뷰 SHA 일치 | 필수 | FAIL | Q01 |
| [C02](cli-mvp-test-results.md#c02) | 호스트 Git 작업의 저장소 실행 코드 격리 | 필수 | FAIL | Q03 |
| [C03](cli-mvp-test-results.md#c03) | Feature 브랜치 이름 충돌의 안전한 실패 | 필수 | PASS | — |
| [D01](cli-mvp-test-results.md#d01) | 기존 테스트 무력화 패치 차단 | 필수 | FAIL | Q04 |
| [D02](cli-mvp-test-results.md#d02) | JUnit 상위 testsuite 오류 검출 | 필수 | FAIL | Q10 |
| [E01](cli-mvp-test-results.md#e01) | 실제 Worker 강제 종료 후 완료 단계 재사용 | 필수 | PASS | — |
| [E02](cli-mvp-test-results.md#e02) | 다른 실행의 잠금 대기 중 취소 반영 | 필수 | FAIL | Q08 |
| [E03](cli-mvp-test-results.md#e03) | 상태 전이 및 DB 스키마 버전 검증 | 필수 | FAIL | Q09 |
| [F01](cli-mvp-test-results.md#f01) | 승인 후 최종 보고서의 상태 일치 | 필수 | FAIL | Q11 |
| [F02](cli-mvp-test-results.md#f02) | 잘못된 YAML 설정의 사용자 오류 처리 | 필수 | FAIL | Q12 |
| [F03](cli-mvp-test-results.md#f03) | Worker 사전 실패의 보고서 생성 | 권장 | FAIL | Q13 |
| [F04](cli-mvp-test-results.md#f04) | Worktree 정리 후 승인 유효성 재확인 | 필수 | FAIL | Q01 |
| [F05](cli-mvp-test-results.md#f05) | 제어 요청의 영속 이벤트 기록 | 권장 | FAIL | Q14 |
| [G01](cli-mvp-test-results.md#g01) | 누락 필드가 있는 리뷰 JSON의 거절 | 필수 | FAIL | Q07 |
| [G02](cli-mvp-test-results.md#g02) | 기준 커밋 고정과 공통 조상 기반 리뷰 Diff | 필수 | PASS | — |
| [G03](cli-mvp-test-results.md#g03) | 승인 시 자동 Push·Merge 미수행 | 필수 | PASS | — |
| [G04](cli-mvp-test-results.md#g04) | 역할별 독립 Codex 프로세스와 실행 옵션 | 필수 | PASS | — |
| [G05](cli-mvp-test-results.md#g05) | 동일 실행의 동시 Worker 중복 실행 차단 | 필수 | PASS | — |
| [G06](cli-mvp-test-results.md#g06) | 재개 전 선행 완료 산출물 검증 | 필수 | FAIL | Q15 |
| [G07](cli-mvp-test-results.md#g07) | SQLite 파일 및 외래 키 정합성 | 필수 | PASS | — |
| [G08](cli-mvp-test-results.md#g08) | 테스트 출력 경로에 추적 소스가 있을 때 차단 | 필수 | PASS | — |
| [G09](cli-mvp-test-results.md#g09) | SQLite 재개방·WAL·외래 키·이벤트 커서 | 필수 | PASS | — |
| [G10](cli-mvp-test-results.md#g10) | 테스트 0건·전체 skip·잘못된 XML 거절 | 필수 | PASS | — |
| [G11](cli-mvp-test-results.md#g11) | 리뷰 입력에서 개발 추론·대화 분리 | 필수 | PASS | — |
| [H01](cli-mvp-test-results.md#h01) | 실제 분리 Worker에서 CLI 취소 처리 | 필수 | PASS | — |
| [H02](cli-mvp-test-results.md#h02) | 패치 경로 탈출·Git 메타데이터·프로덕션 변경 차단 | 필수 | PASS | — |
| [H03](cli-mvp-test-results.md#h03) | 기본 3회 수정 제한과 회차별 산출물 보존 | 필수 | PASS | — |
| [N01](cli-mvp-test-results.md#n01) | 현 버전 실제 Codex 전체 워크플로·Fix 품질 | 필수 | NOT_TESTED | — |
| [N02](cli-mvp-test-results.md#n02) | 현 버전 실제 Codex 역할별 파일·네트워크 제한 집행 | 필수 | NOT_TESTED | — |
| [N03](cli-mvp-test-results.md#n03) | 실제 서버의 인증 만료·사용량 한도 응답 | 권장 | NOT_TESTED | — |
| [N04](cli-mvp-test-results.md#n04) | 모든 Git·파일·DB 경계에서의 강제 종료 복구 | 필수 | NOT_TESTED | — |
| [N05](cli-mvp-test-results.md#n05) | Linux bwrap·Java·JavaScript 도구 체인 | 권장 | NOT_TESTED | — |
| [NA01](cli-mvp-test-results.md#na01) | FastAPI·React·SSE 엔드포인트 자체 | 권장 | NOT_APPLICABLE | — |
| [V01](cli-mvp-test-results.md#v01) | 기존 전체 테스트 재실행 | 필수 | PASS | — |
| [V02](cli-mvp-test-results.md#v02) | 린트 및 포맷 검사 | 권장 | PASS | — |
| [V03](cli-mvp-test-results.md#v03) | 설치된 Codex CLI 옵션 호환성 확인 | 필수 | PASS | — |
| [V04](cli-mvp-test-results.md#v04) | 저장된 Codex 로그인 상태 조회 | 권장 | PASS | — |
| [V05](cli-mvp-test-results.md#v05) | 공통 서비스 재사용 및 상태 JSON 직렬화 | 필수 | PASS | — |
| [V06](cli-mvp-test-results.md#v06) | 회차별 로그 파일 조회 및 역할 필터 | 필수 | PASS | — |

## 후속 작업 경계

이번 작업은 검증과 문서 작성으로 종료합니다. 발견된 구현 문제의 수정은 수행하지 않았으며, 사용자 승인 전 수정 작업을 시작하지 않습니다.
