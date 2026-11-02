# CLI MVP 결함 수정 및 재검증 결과

검증일: 2026-11-02 (Asia/Seoul)  
수정 전 감사 대상: `5f93bde38eda2279c7c51d486cc5a47d65598bfa`  
수정 코드·회귀 테스트 커밋: `99bb906`  
현재 준비도 판단: **CONDITIONAL** — 재현된 결함은 보완했으나 실제 모델 Fix·권한 위반·전 장애 경계·다른 플랫폼 검증은 추가로 필요합니다. 원래의 48개 항목을 모두 다시 실행한 정식 READY 판정은 아닙니다.

## 결과 요약

- 기존 테스트를 포함한 **61개 테스트 통과**, 제외된 테스트 없음.
- 실제 macOS 샌드박스에서 파일 쓰기·네트워크 차단 검사 통과.
- Codex CLI **0.166.0** 실제 E2E: pytest **12개 통과**, 실패 0개, 건너뛰기 0개, 종료 코드 0.
- 실제 독립 리뷰 결과: **Low 1건**. 기본 차단 기준인 Critical·High는 없었으며 Low는 기록한 채 승인 처리했습니다. 지적 0건으로 표시하지 않습니다.
- E2E 상태 `completed`, 승인 SHA와 최종 검증 SHA 일치: `0f3cf0af98aadee07543417b937e06f17b55ca23`.
- 실제 모델 실행 역할은 Planning·Developer·Test·Review입니다. 실제 모델의 Fix 호출은 발생하지 않았고, Fix 반복은 기존 Mock 테스트 및 실패 시나리오로 검증했습니다.
- lint·format·wheel/sdist 빌드 통과.
- FastAPI·React·SSE 신규 구현 없음.

## 결함별 수정과 검증

| 결함 | 중요도 / 기존 심각도 | 수정 내용 | 회귀 검증 및 실제 결과 |
|---|---|---|---|
| Q01 승인 SHA 연결 | 필수 / High | 공통 evidence 검증으로 최신 Test/Review 산출물·캐시·후보 SHA 대조. 보고 전후 외부 변경 차단. 정리 후 Feature ref도 조회 | `test_external_commit_cannot_be_approved`, `test_cleanup_does_not_hide_changed_feature_ref` 통과. 변경된 SHA 승인 거부 |
| Q02 자식 프로세스 생존 | 필수 / High | 부모 wait 결과와 관계없이 잔여 프로세스 그룹 종료. 정상 종료 시 남은 그룹도 정리 | `test_parent_exit_does_not_leave_term_ignoring_child` 통과. SIGTERM 무시 자식 종료 확인 |
| Q03 호스트 Git 필터 | 필수 / High | 각 Git 호출 전에 clean/smudge/process/driver 설정 확인. 실행 가능 필터가 있으면 안전하게 거부. Git 호출 시간 제한 추가 | `test_filter_execution_is_rejected` 통과. 저장소 외부 canary 미생성. 필터 지원을 조용히 우회하지 않음 |
| Q04 테스트 무력화 | 필수 / High | Test Agent도 기존 테스트 파일 수정 금지. 새 테스트 파일 생성만 허용 | `test_existing_test_edits_are_rejected` 및 기존 production/symlink 경계 검사 통과 |
| Q05 민감값 마스킹 | 필수 / High | JSON·인용된 키·Bearer 처리 보강, 구조화 응답·산출물·보고서 정제 | `test_redacts_common_secret_formats` 4개 형식 통과. 실제 비밀 대신 합성 CANARY 사용 |
| Q06 사전 검사 무한 대기 | 필수 / High | Codex 사전 검사도 공통 timeout/stop 실행기 사용. 사전 점검을 Worker 소유권 안으로 이동 | `test_preflight_timeout` 통과. 멈추는 --help 스텁 종료 |
| Q07 불완전 리뷰 JSON | 필수 / High | 필수 필드 집합을 실제 수신 JSON에서 확인하고 strict 타입 검증. 필드 누락을 기본값으로 성공 처리하지 않음 | `test_incomplete_review_json_rejected`와 기존 잘못된 JSON 검사 통과 |
| Q08 대기 작업 취소 | 필수 / Medium | 블로킹 flock 대신 취소 요청을 확인하는 제한 간격 대기 | `test_queue_cancel_is_independent_of_execution_slot` 통과. 다른 실행 잠금 유지 중에도 취소 확정 |
| Q09 상태·DB 버전 | 필수 / Medium | 합법적 상태 전이 및 예상 버전 검증. 스키마 1→2 갱신, 미지원 버전 거부 | `test_state_and_version_validation`, `test_schema_v1_migration_preserves_data` 통과 |
| Q10 JUnit 상위 오류 | 필수 / Medium | suite 수준 오류 수가 testcase 증거와 맞지 않으면 파싱 실패 | `test_suite_error_is_not_success` 및 0건·전체 skip·XML 검사 통과 |
| Q11 승인 후 보고서 | 필수 / Medium | 승인 후 보고서 갱신, 조회 시 DB 기준 재생성 | `test_approval_report_matches_state` 통과. DB와 보고서 모두 completed |
| Q12 YAML 오류 | 필수 / Medium | YAML 구문 오류를 민감 원문 없는 도메인 입력 오류로 변환 | 기존 CLI 오류 테스트와 설정 오류 처리 확인. traceback 대신 설정 오류 메시지 |
| Q13 사전 실패 보고서 | 권장 / Medium | 사전 점검을 Runner 안에서 수행하고 모든 실패 경로에 보고서 생성 | `test_preflight_failure_has_report` 통과. 실행 파일 누락 원인 조회 가능 |
| Q14 제어·로그 이벤트 | 권장 / Medium | 버전 있는 제어·단계 이벤트, 단계·회차·산출물 참조 저장. 서비스에 파일별 바이트 커서 조회 추가 | `test_control_events_have_cursor`, `test_log_cursor_replay`, `test_log_cursor_preserves_korean` 통과. SSE 서버는 미구현 |
| Q15 재개 전 증거 검증 | 필수 / Medium | 모든 완료 체크포인트의 체크섬·JSON·출력 SHA를 resume와 실행 진입에서 확인 | `test_resume_detects_prior_evidence_corruption` 통과. 추가 역할 호출 전 거절 |

제품 코드 위치는 `src/dev_orchestra/infrastructure/evidence.py`, `process.py`, `codex.py`, `git.py`, `locking.py`, `store.py`, `test_runner.py`, `application/service.py`, `orchestration/runner.py`, `config.py`, `agents/policy.py` 및 `worker.py`입니다. 회귀 테스트는 `tests/test_remediation.py`에 있습니다.

## 실제 강제 종료 경계

`test_abrupt_exit_at_durable_boundaries`는 별도 Python 프로세스를 사용합니다. 예외를 잡아 정상 종료하는 대신 `os._exit(17)`로 finally·정리 경로를 건너뛰었습니다.

| 장애 위치 | 실제 결과 |
|---|---|
| Developer Git 커밋 직후, DB SHA 확인 전 | 코드 변경을 보존하고 HEAD 불일치를 감지해 needs_input. 자동 reset·재개발 없음 |
| Developer 완료 산출물·체크포인트 저장 직후 | 완료 Developer를 재실행하지 않고 Test/Review만 실행. waiting_approval 도달 |

과거 감사의 SIGKILL 시나리오에 더해 위 두 경계를 추가했습니다. 부분 파일 쓰기·모든 SQL/파일시스템 경계·전원 장애까지 모두 검증했다는 뜻은 아닙니다.

## 실행 명령과 로그

```sh
DEVORCHESTRA_TEST_SANDBOX=1 .venv/bin/python -m pytest -q
.venv/bin/ruff check src tests scripts
.venv/bin/ruff format --check src tests scripts
UV_CACHE_DIR="$PWD/.uv-cache" .tools/uv build --offline
.venv/bin/python scripts/e2e.py
```

- 최종 전체 테스트 로그: `/private/tmp/devorchestra-remediation-tests-final.log` — **61 passed in 27.00s**.
- 실제 Codex E2E 로그: `/private/tmp/devorchestra-remediation-e2e.log` — **REAL E2E PASSED**.
- 실제 E2E 산출물: `/var/folders/n1/ktxrq6sj1r7d_zq3k5s840s80000gn/T/devorchestra-e2e-u1yxweei/`.
- lint 결과: **All checks passed**. format 결과: **26 files already formatted**.
- 패키지: `dist/dev_orchestra-0.1.0-py3-none-any.whl`, `dist/dev_orchestra-0.1.0.tar.gz` 빌드 성공. 캐시 디렉터리 위치에 대한 uv 경고는 빌드 실패가 아닙니다.

수정 도중 처음 발견된 기존 테스트 실패 5건과 새 마스킹 테스트 실패 1건도 원인을 수정한 후 재검증했습니다. 실패 테스트를 삭제하지 않았습니다. 기존 강제 종료 상태 모의 테스트 1곳은 합법적 planning→developing 전이를 거쳐 fixture를 구성하도록 보완했으며, 결과 기대값은 유지했습니다.

## 남아 있는 제한과 후속 검증

- N01: 현 버전 정상 E2E는 통과했으나 실제 모델의 Fix 반복·복잡한 PRD 품질은 미검증입니다.
- N02: 정상 실제 Codex 세션과 실제 테스트 샌드박스는 확인했습니다. 모델이 의도적으로 영역 밖 읽기·쓰기·네트워크·MCP 권한을 시도하는 전체 검증은 하지 않았습니다.
- N03: 실제 인증 실패·만료·429는 Mock 처리만 확인했습니다. 실제 계정 상태를 변경하지 않았습니다.
- N04: 위 두 경계를 보강했으나 모든 장애 지점의 자동 복구를 보장하지 않습니다. 커밋 직후 DB 확인 전 오류는 보존 후 수동 판단이 필요합니다.
- N05: Linux bwrap 및 Java/JavaScript 실행 도구 체인은 미검증입니다.
- Git 필터가 있는 저장소와 기존 테스트 파일 수정을 요구하는 에이전트 결과는 기본 거부합니다. LFS나 테스트 수정이 필요한 작업에는 별도 허용 정책·검토 절차가 필요합니다.
- 일반적인 로그 비밀값 형식에 대한 회귀 검증이며, 알려지지 않은 모든 비밀 형식을 식별하거나 완전한 악성 코드 격리를 보장하지는 않습니다.

따라서 이번 결과는 **알려진 결함 수정 및 회귀 검증 완료**입니다. 모든 필수 항목을 통과했다는 READY 선언과 구분하며, GUI 실행·승인 API를 연결하기 전에 위 미검증 항목을 추가 확인하는 것을 권장합니다.
