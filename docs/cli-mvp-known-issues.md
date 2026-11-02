> 후속 수정: 이 문서는 `5f93bde` 대상의 수정 전 감사 기록입니다. `99bb906`에서 수행한 결함 수정과 재검증은 [수정 및 검증 결과](cli-mvp-remediation-results.md)를 참고하세요. 아래 FAIL·NOT_READY 집계는 과거 평가를 보존한 것입니다.

# CLI MVP 알려진 문제

검증일: 2026-11-02 (Asia/Seoul)  
대상 커밋: `5f93bde38eda2279c7c51d486cc5a47d65598bfa`  
결함 15건: Critical 0 / High 7 / Medium 8. 발견한 문제는 수정하지 않았습니다.

각 항목의 관찰값·명령·중요도·품질 상태는 [상세 테스트 결과](cli-mvp-test-results.md)의 같은 ID를 기준으로 합니다. 이 문서는 동일한 결함을 품질 항목별로 중복 집계하지 않고 원인별로 묶습니다.

## 심각도와 우선순위

- Critical: 이번 검증에서 해당 수준으로 확정한 결함 없음.
- High: 검증·승인 신뢰성, 프로세스 생명주기, 민감정보 및 호스트 실행 경계가 깨지는 결함.
- Medium: 입력·표시·이력·운영 복구 계약이 불완전하거나 방어 검증이 늦는 결함.

## Q01 — 검증 SHA와 승인 SHA의 연결 누락 및 정리 후 유효성 확인 누락

- 심각도: **High**
- 중요도: 필수
- 상태: **FAIL**
- 검증 항목: [C01](cli-mvp-test-results.md#c01), [F04](cli-mvp-test-results.md#f04)
- 검증 방법: 실제 pytest 통과 후 reporting 시점에 외부 커밋을 삽입하는 계측 Runner 사용. 제품의 report/approve 구현은 그대로 호출. 승인 후 실제 pytest를 재실행. 정상 승인→cleanup 후 임시 저장소의 Feature ref를 새 커밋으로 이동. status의 현재 결과 상태 확인. 과거 승인 이력의 SHA는 유지돼야 하지만 현재 결과의 유효성과 구분되어야 함.
- 실행한 명령어: `CMD-AUDIT`, `CMD-STALE`
- 관련 파일 및 코드 위치: [src/dev_orchestra/orchestration/runner.py:99](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/orchestration/runner.py#L99), [src/dev_orchestra/application/service.py:126](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/application/service.py#L126), [src/dev_orchestra/application/service.py:151](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/application/service.py#L151), [src/dev_orchestra/application/service.py:159](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/application/service.py#L159)
- 실패 원인: 완료 경로는 현재 HEAD를 final_sha로 저장하며 test_result.sha/review_result.sha와 대조하지 않는다. approve도 해당 비교가 없다. Worktree가 없으면 status는 Feature ref 변경을 검사하지 않는다.
- 필요한 수정 사항: 검증 증거를 동일한 후보 SHA·정책·입력 해시에 연결하고 보고서 생성·승인 직전에 재확인한다. 달라지면 검증을 무효화하고 Test/Review부터 재실행한다. cleanup 이후에는 Feature ref와 과거 승인 SHA를 구분해 표시한다.
- 수정 완료 조건: 실제 테스트 통과 후 외부 커밋을 넣으면 승인 거부. 해당 SHA 재검증 후에만 승인. 정리 후 ref 변경도 감지.

실제 결과:

```text
{
  "C01": {
    "approved_state": "completed",
    "old_test_passed": true,
    "test_sha": "fd12377f09e0d1572a48b01ad3d4a295bac48a9a",
    "final_sha": "7e114fda44f419974a565a6eaf72065878b78f93",
    "actual_retest_passed": false
  },
  "F04": {
    "status_after_feature_ref_changed": "completed",
    "feature_sha": "1cad495587c07e2ffc2dd9220801fb48abd1849f",
    "approved_sha": "0ec758f37cb68d89a5dea41771c54112e4c6a000",
    "worktree_exists": false
  }
}
```

## Q02 — 시간 초과 후 SIGTERM을 무시하는 자식이 살아남음

- 심각도: **High**
- 중요도: 필수
- 상태: **FAIL**
- 검증 항목: [B04](cli-mvp-test-results.md#b04)
- 검증 방법: 자식은 SIGTERM을 무시하고 부모는 기본 종료하도록 구성. 시간 초과 후 자식 생존과 finished 마커 검사. 검증 후 남은 자식 PID는 직접 종료.
- 실행한 명령어: `CMD-AUDIT`
- 관련 파일 및 코드 위치: [src/dev_orchestra/infrastructure/process.py:91](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/infrastructure/process.py#L91), [src/dev_orchestra/application/service.py:88](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/application/service.py#L88)
- 실패 원인: SIGTERM 후 부모 wait가 빨리 반환하면 프로세스 그룹의 잔여 자식에 SIGKILL을 보내지 않는다. 그 상태에서도 marker.finished=true가 기록된다.
- 필요한 수정 사항: 부모 종료와 그룹 전체 종료를 분리해 추적하고 유예 시간 후 잔여 그룹을 종료한다. 프로세스 식별·소유권과 실제 종료 확인을 기록해 resume/cancel에 사용한다.
- 수정 완료 조건: 부모는 먼저 종료하고 자식은 SIGTERM을 무시하는 재현에서도 모든 검증용 자식이 사라져야 한다.

실제 결과:

```text
{
  "B04": {
    "error": "process timeout",
    "descendant_alive_after_timeout": true,
    "marker_finished": true,
    "cleanup": "검증용 자식 PID를 종료함"
  }
}
```

## Q03 — 호스트 Git clean filter가 샌드박스 밖에서 실행됨

- 심각도: **High**
- 중요도: 필수
- 상태: **FAIL**
- 검증 항목: [C02](cli-mvp-test-results.md#c02)
- 검증 방법: 임시 저장소에 clean filter driver를 설정하고 .gitattributes가 이를 참조하도록 구성. Git.commit이 임시 저장소 외부 canary 파일을 쓰는지 확인. 실제 사용자 파일·네트워크는 사용하지 않음.
- 실행한 명령어: `CMD-AUDIT`
- 관련 파일 및 코드 위치: [src/dev_orchestra/infrastructure/git.py:14](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/infrastructure/git.py#L14), [src/dev_orchestra/infrastructure/git.py:56](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/infrastructure/git.py#L56)
- 실패 원인: Git 호출은 hooks·서명·fsmonitor만 비활성화한다. 저장소에 설정된 clean filter는 git add에서 그대로 실행된다. 조건은 해당 filter driver가 저장소 설정에 존재하는 경우다. PRD만으로 임의 드라이버를 새로 설정할 수 있다고 입증한 것은 아니다.
- 필요한 수정 사항: 저장소 filter·외부 프로그램·환경 설정의 신뢰 경계를 정의한다. 호스트 Git도 파일·네트워크 제한 하에서 실행하거나 미승인 filter 사용을 사전 거절한다. 필요한 LFS 등은 명시적 허용 정책으로 분리한다.
- 수정 완료 조건: 필터가 설정된 임시 저장소에서도 허용 영역 밖 canary 쓰기·통신이 차단되거나 실행 전 안전하게 거절돼야 한다.

실제 결과:

```text
{
  "C02": {
    "unsandboxed_clean_filter_executed": true,
    "write_location": "임시 저장소 외부의 검증용 파일",
    "precondition": "저장소에 clean filter driver 설정이 존재함"
  }
}
```

## Q04 — Test Agent가 기존 실패 테스트를 무력화할 수 있음

- 심각도: **High**
- 중요도: 필수
- 상태: **FAIL**
- 검증 항목: [D01](cli-mvp-test-results.md#d01)
- 검증 방법: 기존 assert False 테스트의 실제 실패를 확인한 뒤 tester 역할로 assert True 변경 패치 적용. 같은 명령으로 실제 pytest 재실행.
- 실행한 명령어: `CMD-AUDIT`
- 관련 파일 및 코드 위치: [src/dev_orchestra/agents/policy.py:52](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/agents/policy.py#L52), [src/dev_orchestra/orchestration/runner.py:173](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/orchestration/runner.py#L173)
- 실패 원인: tester 역할에는 경로만 제한하고 기존 테스트 내용 변경은 허용한다. assert False를 assert True로 바꾸는 패치가 적용돼 실제 실패가 성공으로 바뀌었다. Fix 뒤에도 Test Agent가 다시 실행되므로 Fix의 테스트 보호만으로 충분하지 않다.
- 필요한 수정 사항: 기존 테스트의 삭제·약화·skip/xfail 추가를 호스트 정책으로 제한하고, 허용 변경은 명시적 검토로 전환한다. 테스트 기준 스냅샷을 고정하고 독립 리뷰 증거를 남긴다.
- 수정 완료 조건: 기존 실패 assertion을 제거·상수 성공으로 치환하는 패치가 자동 통과하지 않아야 한다. 의미적 완전 탐지가 불가능한 범위는 명시한다.

실제 결과:

```text
{
  "D01": {
    "before_passed": false,
    "after_passed": true,
    "test_agent_patch_accepted": true,
    "production_unchanged": true
  }
}
```

## Q05 — 일반 JSON token·Bearer 값이 로그에 그대로 남음

- 심각도: **High**
- 중요도: 필수
- 상태: **FAIL**
- 검증 항목: [B03](cli-mvp-test-results.md#b03)
- 검증 방법: 실제 비밀값 대신 합성 canary를 JSON token 및 Authorization Bearer에 넣어 execute 출력 로그 검사.
- 실행한 명령어: `CMD-AUDIT`
- 관련 파일 및 코드 위치: [src/dev_orchestra/infrastructure/process.py:18](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/infrastructure/process.py#L18), [src/dev_orchestra/infrastructure/process.py:53](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/infrastructure/process.py#L53)
- 실패 원인: 정규식은 키 다음의 따옴표를 처리하지 못하고 일반 Bearer 헤더도 다루지 않는다. 알려진 sk-/PAT 접두사 마스킹과 별개로 합성 민감값이 로그에 저장됐다.
- 필요한 수정 사항: 구조화 로그에서 민감 필드를 먼저 제거하고 JSON·헤더·텍스트 로그를 일관되게 처리한다. 알고 있는 환경 비밀값과 분할 출력도 검증하고 response/result/report 저장 경로도 점검한다.
- 수정 완료 조건: 가짜 token/password/Authorization 값을 다양한 JSON·인용·분할 출력으로 넣어도 저장·조회 결과에 원문이 없어야 한다.

실제 결과:

```text
{
  "B03": {
    "exit_code": 0,
    "synthetic_secret_persisted": true,
    "format": "JSON token / Authorization Bearer"
  }
}
```

## Q06 — Codex 사전 기능 검사가 타임아웃·취소 제어 밖에 있음

- 심각도: **High**
- 중요도: 필수
- 상태: **FAIL**
- 검증 항목: [B02](cli-mvp-test-results.md#b02)
- 검증 방법: --help에서 멈추는 Mock Codex로 timeout_seconds=1을 설정. 1.5초 뒤 프로세스 생존을 확인하고 검증 프로세스 그룹을 정리.
- 실행한 명령어: `CMD-AUDIT`
- 관련 파일 및 코드 위치: [src/dev_orchestra/infrastructure/codex.py:26](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/infrastructure/codex.py#L26), [src/dev_orchestra/worker.py:19](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/worker.py#L19)
- 실패 원인: CodexClient.check의 subprocess.run에는 timeout이 없고 Worker가 실행 잠금 및 제어 확인 전에 호출한다. 실행 타임아웃을 1초로 설정해도 --help 프로세스가 계속 대기했다.
- 필요한 수정 사항: 사전 점검·Git 작업을 포함한 모든 외부 호출에 제한 시간과 취소 제어를 적용한다. Worker 소유권과 상태 검증을 사전 점검보다 먼저 확보한다.
- 수정 완료 조건: --help에서 멈추는 스텁도 설정된 시간 안에 종료되며 cancel이 해당 프로세스와 상태를 정리해야 한다.

실제 결과:

```text
{
  "B02": {
    "still_running_after_seconds": 1.5,
    "timeout_configured_seconds": 1,
    "alive": true
  }
}
```

## Q07 — 불완전한 리뷰 JSON을 문제 없음으로 해석

- 심각도: **High**
- 중요도: 필수
- 상태: **FAIL**
- 검증 항목: [G01](cli-mvp-test-results.md#g01)
- 검증 방법: Mock Codex가 summary만 있는 JSON 반환. 출력 JSON Schema의 required 목록과 host Pydantic 검증 결과 비교.
- 실행한 명령어: `CMD-EXTRA`
- 관련 파일 및 코드 위치: [src/dev_orchestra/domain/models.py:53](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/domain/models.py#L53), [src/dev_orchestra/infrastructure/codex.py:11](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/infrastructure/codex.py#L11), [src/dev_orchestra/infrastructure/codex.py:73](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/infrastructure/codex.py#L73)
- 실패 원인: 출력 JSON Schema에서는 모든 필드를 required로 만들지만 host AgentResult에는 기본값이 있다. summary만 있는 응답도 issues=[]로 채워진다. CLI/provider의 스키마 제약이 항상 성공한다고 가정하는 경계다.
- 필요한 수정 사항: 역할별 필수 필드와 완료 상태를 명시하고 실제 수신 JSON을 동일한 필수 스키마로 검증한다. 리뷰 필드 누락·실행 미완료·거절 응답은 통과 판정과 구분한다.
- 수정 완료 조건: summary-only 및 issues 누락 응답은 프로토콜 오류로 처리하고 waiting_approval에 도달하지 않아야 한다.

실제 결과:

```text
{
  "G01": {
    "accepted_incomplete_json": true,
    "defaulted_issues": [],
    "supplied_fields": [
      "summary"
    ]
  }
}
```

## Q08 — 잠금 대기 Worker는 다른 실행이 끝나야 취소됨

- 심각도: **Medium**
- 중요도: 필수
- 상태: **FAIL**
- 검증 항목: [E02](cli-mvp-test-results.md#e02)
- 검증 방법: DB Worker 잠금을 별도 소유한 채 대기 Worker를 실행하고 cancel 요청. 잠금 유지 중 상태와 생존을 검사한 뒤 해제.
- 실행한 명령어: `CMD-AUDIT`
- 관련 파일 및 코드 위치: [src/dev_orchestra/infrastructure/locking.py:16](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/infrastructure/locking.py#L16), [src/dev_orchestra/orchestration/runner.py:40](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/orchestration/runner.py#L40)
- 실패 원인: 전역 DB·저장소 flock을 블로킹으로 대기하며 제어 요청을 확인하지 않는다. 잠금 대기 자체는 실행 타임아웃에 포함되지 않는다.
- 필요한 수정 사항: 취소 가능한 대기·작업 claim/lease 구조를 도입하고 큐 작업 취소는 실행 슬롯과 무관하게 확정할 수 있게 한다.
- 수정 완료 조건: 다른 실행이 계속 잠금을 보유해도 대기 작업은 제한된 시간 안에 cancelled로 전환돼야 한다.

실제 결과:

```text
{
  "E02": {
    "while_other_run_holds_lock": {
      "state": "queued",
      "control": "cancel",
      "worker_alive": true
    },
    "after_releasing_other_run": "cancelled"
  }
}
```

## Q09 — 상태 전이 및 DB 스키마 버전 검증 부재

- 심각도: **Medium**
- 중요도: 필수
- 상태: **FAIL**
- 검증 항목: [E03](cli-mvp-test-results.md#e03)
- 검증 방법: Store.update로 정의되지 않은 상태를 입력. schema_version=999인 임시 DB를 Store로 열어 거절 여부 검사. 두 세부 검사가 모두 실패.
- 실행한 명령어: `CMD-AUDIT`
- 관련 파일 및 코드 위치: [src/dev_orchestra/infrastructure/store.py:17](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/infrastructure/store.py#L17), [src/dev_orchestra/infrastructure/store.py:78](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/infrastructure/store.py#L78), [src/dev_orchestra/domain/models.py:14](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/domain/models.py#L14)
- 실패 원인: State enum은 Store 갱신을 제한하지 않고 임의 상태 문자열이 수용된다. schema_version은 버전 1을 삽입할 뿐 지원 여부나 마이그레이션을 검사하지 않아 999와 1이 공존했다.
- 필요한 수정 사항: 합법적 전이표·예상 상태/버전 조건 갱신을 서비스와 저장소 경계에 적용한다. 단일 현재 버전과 순차 마이그레이션, 미지원 미래 버전 거절 정책을 마련한다.
- 수정 완료 조건: 불법 전이·알 수 없는 상태·미지원 DB 버전이 거절되고 업그레이드 전후 이력이 보존돼야 한다.

실제 결과:

```text
{
  "E03": {
    "accepted_state": "invented_state",
    "schema_versions": [
      1,
      999
    ],
    "future_schema_rejected": false
  }
}
```

## Q10 — JUnit testsuite 수준 오류를 놓침

- 심각도: **Medium**
- 중요도: 필수
- 상태: **FAIL**
- 검증 항목: [D02](cli-mvp-test-results.md#d02)
- 검증 방법: testsuite errors=1, 하위 testcase에는 오류가 없는 보고서를 파싱. 실패로 취급하는지 검사.
- 실행한 명령어: `CMD-AUDIT`
- 관련 파일 및 코드 위치: [src/dev_orchestra/infrastructure/test_runner.py:53](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/infrastructure/test_runner.py#L53)
- 실패 원인: testcase의 failure/error만 집계하고 testsuite의 errors/failures 및 집계 일관성을 검사하지 않는다. 상위 errors=1 보고서가 failed=0으로 해석됐다.
- 필요한 수정 사항: 지원하는 JUnit 방언을 정하고 suite/case 집계 불일치와 전역 오류를 실행 실패 또는 파싱 실패로 처리한다.
- 수정 완료 조건: testsuite errors=1, 글로벌 setup 오류, 중첩 suite 집계 불일치가 통과 처리되지 않아야 한다.

실제 결과:

```text
{
  "D02": {
    "declared_suite_errors": 1,
    "parsed": {
      "total": 1,
      "failed": 0,
      "skipped": 0,
      "failures": []
    }
  }
}
```

## Q11 — 승인 후 보고서가 여전히 승인 대기로 표시됨

- 심각도: **Medium**
- 중요도: 필수
- 상태: **FAIL**
- 검증 항목: [F01](cli-mvp-test-results.md#f01)
- 검증 방법: 정상 워크플로 승인 후 DB 상태와 report 명령이 읽는 final-report.md 내용 비교.
- 실행한 명령어: `CMD-AUDIT`
- 관련 파일 및 코드 위치: [src/dev_orchestra/application/service.py:126](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/application/service.py#L126), [src/dev_orchestra/application/service.py:174](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/application/service.py#L174), [src/dev_orchestra/orchestration/runner.py:245](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/orchestration/runner.py#L245)
- 실패 원인: approve는 DB만 변경하고 report는 승인 이전 final-report.md를 그대로 반환한다.
- 필요한 수정 사항: 승인·취소·무효화 후 보고서/metadata를 다시 생성하거나 조회 시 DB 기준 승인 정보를 합성한다.
- 수정 완료 조건: 동일 실행의 status와 report가 승인 상태·SHA·승인 필요 여부에 대해 일치해야 한다.

실제 결과:

```text
{
  "F01": {
    "database_state": "completed",
    "report_still_waiting_approval": true
  }
}
```

## Q12 — YAML 구문 오류가 CLI 사용자 오류로 변환되지 않음

- 심각도: **Medium**
- 중요도: 필수
- 상태: **FAIL**
- 검증 항목: [F02](cli-mvp-test-results.md#f02)
- 검증 방법: project: [ 로 끝나는 잘못된 YAML을 전달해 CliRunner 종료 코드·출력·예외 유형 확인.
- 실행한 명령어: `CMD-AUDIT`
- 관련 파일 및 코드 위치: [src/dev_orchestra/cli.py:16](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/cli.py#L16), [src/dev_orchestra/config.py:89](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/config.py#L89)
- 실패 원인: guarded가 yaml.YAMLError를 잡지 않아 CliRunner에서는 ParserError와 빈 출력이 발생했다. 실제 터미널에서는 상위 실행 환경에 따라 traceback이 노출될 수 있다.
- 필요한 수정 사항: 설정 로더 오류를 도메인 입력 오류로 정규화하고 민감한 설정 원문을 노출하지 않는 메시지와 종료 코드를 제공한다.
- 수정 완료 조건: 잘못된 YAML에 짧은 사용자 메시지와 종료 코드 1을 반환하고 traceback·민감값을 노출하지 않아야 한다.

실제 결과:

```text
{
  "F02": {
    "exit_code": 1,
    "output": "",
    "exception_type": "ParserError"
  }
}
```

## Q13 — Worker 사전 점검 실패 시 보고서가 없음

- 심각도: **Medium**
- 중요도: 권장
- 상태: **FAIL**
- 검증 항목: [F03](cli-mvp-test-results.md#f03)
- 검증 방법: 존재하지 않는 Codex 실행 파일을 설정하고 실제 별도 Worker 실행. failed 상태 및 final-report.md 존재 검사.
- 실행한 명령어: `CMD-AUDIT`
- 관련 파일 및 코드 위치: [src/dev_orchestra/worker.py:19](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/worker.py#L19), [src/dev_orchestra/application/service.py:65](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/application/service.py#L65)
- 실패 원인: worker.main의 예외 경로는 Store만 failed로 갱신하며 Runner.report를 호출하지 않는다.
- 필요한 수정 사항: 사전 실패를 포함해 모든 실행 종료 경로에 공통 실패 결과·보고서 생성을 적용한다.
- 수정 완료 조건: 실행 파일 누락·사전 점검 실패도 원인과 시점을 report로 조회할 수 있어야 한다.

실제 결과:

```text
{
  "F03": {
    "state": "failed",
    "error": "[Errno 2] No such file or directory: '/private/tmp/devorchestra-readiness-20261102-hrFzNO/does-not-exist'",
    "report_exists": false
  }
}
```

## Q14 — 취소 요청·로그·단계 정보를 위한 이벤트 계약 부족

- 심각도: **Medium**
- 중요도: 권장
- 상태: **FAIL**
- 검증 항목: [F05](cli-mvp-test-results.md#f05)
- 검증 방법: cancel 제어 요청 전후 events 커서 조회. 완료 상태 이벤트와 별개로 요청 이벤트가 남는지 검사.
- 실행한 명령어: `CMD-AUDIT`
- 관련 파일 및 코드 위치: [src/dev_orchestra/infrastructure/store.py:95](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/infrastructure/store.py#L95), [src/dev_orchestra/infrastructure/store.py:131](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/infrastructure/store.py#L131)
- 실패 원인: control 갱신은 이벤트를 쓰지 않는다. 저장된 이벤트는 주로 state 문자열이고 step_id/iteration/로그 커서가 없다.
- 필요한 수정 사항: 명령 요청·수락·종료, 단계·회차·로그 참조를 버전 있는 이벤트로 저장한다. GUI의 SSE는 이를 커서로 조회하는 어댑터로 구현한다.
- 수정 완료 조건: 재접속 뒤 취소 요청과 단계별 로그 이력을 빠짐없이 재생할 수 있어야 한다. SSE 구현 자체는 이번 범위 밖이다.

실제 결과:

```text
{
  "F05": {
    "events_before": 0,
    "events_after_cancel_request": 0,
    "control": "cancel"
  }
}
```

## Q15 — 재개가 선행 완료 산출물 변조를 늦게 발견함

- 심각도: **Medium**
- 중요도: 필수
- 상태: **FAIL**
- 검증 항목: [G06](cli-mvp-test-results.md#g06)
- 검증 방법: reviewer에서 실패한 실행의 완료된 tester 산출물을 변조하고 resume. reviewer 재호출 전 변조 감지 여부 확인. 승인 단계에서는 변조가 차단되는지도 별도 확인.
- 실행한 명령어: `CMD-EXTRA`
- 관련 파일 및 코드 위치: [src/dev_orchestra/orchestration/runner.py:109](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/orchestration/runner.py#L109), [src/dev_orchestra/application/service.py:80](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/application/service.py#L80), [src/dev_orchestra/application/service.py:142](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/application/service.py#L142)
- 실패 원인: 다음 실행 역할에 해당하는 완료 단계만 검사하고 이전 tester 등 선행 증거는 approve까지 검증하지 않는다. 변조 후 reviewer가 다시 호출되고 waiting_approval에 도달했다. 승인은 정상적으로 차단됐다.
- 필요한 수정 사항: resume 시 전체 의존 체크포인트의 SHA·입력 해시·스키마·체크섬을 먼저 검증하고 유효성 손상 지점부터 재실행하거나 개입을 요청한다.
- 수정 완료 조건: 변조된 선행 산출물이 있으면 추가 모델 호출과 승인 대기 진입 전에 탐지돼야 한다.

실제 결과:

```text
{
  "G06": {
    "resumed_calls": [
      "reviewer"
    ],
    "state_before_approval": "waiting_approval",
    "approval_blocked": true
  }
}
```

## 미검증 위험

N01~N05는 결함 확정과 구분합니다. 실제 Codex 0.166.0의 전체 실행, 모델에 의한 권한 위반 시도, 실제 인증 만료/한도 응답, 모든 강제 종료 경계, Linux·다른 도구 체인을 통과로 표시하지 않았습니다. 기존 README에 명시된 광범위한 읽기 허용과 비밀 파일 접근 제한의 한계도 그대로 존재합니다.

## 처리 경계

위 수정 방향은 권고안입니다. 사용자 승인을 받기 전에는 제품 코드와 기존 테스트를 수정하지 않습니다.
