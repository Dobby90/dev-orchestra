> 후속 수정: 이 문서는 `5f93bde` 대상의 수정 전 감사 기록입니다. `99bb906`에서 수행한 결함 수정과 재검증은 [수정 및 검증 결과](cli-mvp-remediation-results.md)를 참고하세요. 아래 FAIL·NOT_READY 집계는 과거 평가를 보존한 것입니다.

# CLI MVP 상세 테스트 결과

검증일: 2026-11-02 (Asia/Seoul)  
대상 커밋: `5f93bde38eda2279c7c51d486cc5a47d65598bfa`  
요약: PASS 26 / FAIL 16 / NOT_TESTED 5 / NOT_APPLICABLE 1, 총 48개 품질 항목.

## 실행 근거와 보관 위치

검증용 임시 디렉터리: `/private/tmp/devorchestra-readiness-20261102-hrFzNO`.

원시 로그·JUnit XML·결과 JSON과 재현 스크립트는 해당 디렉터리에 보존했습니다. 운영체제의 임시 파일 정리 대상이므로 핵심 관찰값과 재현 스크립트 원문은 이 문서에도 기록합니다. 실제 자격 증명은 사용하지 않았고 민감값 검사는 합성 canary로 수행했습니다.

| 파일 | 내용 |
|---|---|
| existing.log / existing.xml | 일반 기존 pytest: 37 passed, 1 skipped |
| native.log / native.xml | 실제 macOS 샌드박스 포함 기존 pytest: 38 passed |
| audit.log / results.json | 주 재현 22개 |
| extra.log / extra-results.json | 추가 재현 11개 |
| final-probes.log / final-results.json | Worker·패치·회차 및 SHA 정밀 재현의 첫 시도 |
| stale-proof-final.log / stale-proof-final.json | 출력 경로 fixture를 바로잡은 실제 pytest 기반 C01 재검증 |
| codex-exec-help.log / codex-global-help.log | 설치된 Codex 0.166.0 도움말 |

### 검증 도구 자체의 오류 처리

C01 정밀 재현 첫 시도에서는 외부 커밋 fixture가 build/JUnit 파일까지 추적해 후속 재테스트가 `output path contains tracked source files`로 차단됐습니다. 해당 시도는 `NOT_TESTED`로 기록됐고 로그를 보존했습니다. **구현은 수정하지 않고 검증용 외부 커밋이 output_paths를 제외하도록 조정**한 뒤 새 임시 저장소에서 재실행했습니다. 최종 C01은 실제 테스트 통과→외부 코드 변경→승인 허용→실제 재테스트 실패를 확인한 FAIL입니다. 첫 도구 오류를 제품 통과나 제품 결함으로 집계하지 않았습니다.

## 실제 실행 명령

명령의 작업 디렉터리는 프로젝트 루트입니다. `CMD-*`는 아래 실행 명령 묶음의 참조 ID이며 각 항목에서 이를 인용합니다. 스크립트의 각 함수가 개별 검증 항목을 수행합니다. 기존 임시 디렉터리에서 재현 스크립트를 그대로 다시 실행하면 fixture 디렉터리 충돌이 나므로 재실행 시 새 임시 디렉터리에 아래 스크립트를 복사해야 합니다.

### CMD-BASE

```sh
.venv/bin/python -m pytest -v --junitxml=/private/tmp/devorchestra-readiness-20261102-hrFzNO/existing.xml
DEVORCHESTRA_TEST_SANDBOX=1 .venv/bin/python -m pytest -v --junitxml=/private/tmp/devorchestra-readiness-20261102-hrFzNO/native.xml
```

### CMD-LINT

```sh
.venv/bin/ruff check src tests scripts
.venv/bin/ruff format --check src tests scripts
```

### CMD-CODEX

```sh
codex --version
codex login status
codex exec --help
codex --help
.venv/bin/python -c "from dev_orchestra.config import Codex; from dev_orchestra.infrastructure.codex import CodexClient; CodexClient(Codex()).check()"
```

### CMD-AUDIT

```sh
PYTHONPATH="$PWD/src:/private/tmp/devorchestra-readiness-20261102-hrFzNO" .venv/bin/python /private/tmp/devorchestra-readiness-20261102-hrFzNO/audit.py
```

### CMD-EXTRA

```sh
PYTHONPATH="$PWD/src:/private/tmp/devorchestra-readiness-20261102-hrFzNO" .venv/bin/python /private/tmp/devorchestra-readiness-20261102-hrFzNO/extra.py
```

### CMD-FINAL

```sh
PYTHONPATH="$PWD/src:/private/tmp/devorchestra-readiness-20261102-hrFzNO" .venv/bin/python /private/tmp/devorchestra-readiness-20261102-hrFzNO/final_probes.py
```

### CMD-STALE

```sh
PYTHONPATH="$PWD/src:/private/tmp/devorchestra-readiness-20261102-hrFzNO" .venv/bin/python - <<'PROBE'
from final_probes import *
record('C01', native_stale)
(ROOT/'stale-proof-final.json').write_text(json.dumps(RESULTS, ensure_ascii=False, indent=2))
PROBE
```

### CMD-SOURCE

```sh
 .venv/bin/python - <<'SOURCE'
from pathlib import Path
from dev_orchestra.config import Codex,Config
from dev_orchestra.infrastructure.codex import CodexClient
from dev_orchestra.application.service import RunService
import ast,json
root=Path('/private/tmp/devorchestra-readiness-20261102-hrFzNO')
CodexClient(Codex()).check()
config_path=next((root/'flow-success'/'runs').glob('*/config.json'))
s=RunService(Config.model_validate_json(config_path.read_text()));r=s.store.list()[0]['id']
status=s.status(r);json.dumps(status);paths=s.log_paths(r,'tester')
service_imports=[n.module for n in ast.walk(ast.parse(Path('src/dev_orchestra/application/service.py').read_text())) if isinstance(n,ast.ImportFrom)]
print('서비스 직렬화 성공, 로그',len(paths),sum(p.stat().st_size for p in paths),'바이트')
SOURCE
```

CMD-SOURCE는 다음 Python 호출을 수행한 heredoc 검사입니다. 명령 원문을 재구성할 때는 임시 디렉터리를 해당 실행 위치로 지정합니다.

```python
from pathlib import Path
import ast, json
from dev_orchestra.config import Codex, Config
from dev_orchestra.infrastructure.codex import CodexClient
from dev_orchestra.application.service import RunService
root = Path("/private/tmp/devorchestra-readiness-20261102-hrFzNO")
CodexClient(Codex()).check()
config_path = next((root / "flow-success" / "runs").glob("*/config.json"))
service = RunService(Config.model_validate_json(config_path.read_text()))
run_id = service.store.list()[0]["id"]
json.dumps(service.status(run_id))
paths = service.log_paths(run_id, "tester")
imports = [n.module for n in ast.walk(ast.parse(Path("src/dev_orchestra/application/service.py").read_text())) if isinstance(n, ast.ImportFrom)]
assert not any(module and module.startswith("typer") for module in imports)
print(len(paths), sum(path.stat().st_size for path in paths))
```

## 기존 테스트의 개별 결과

아래는 `native.xml`의 실제 testcase 목록입니다. 기존 테스트를 삭제·변경하거나 실패를 숨기지 않았습니다.

| 기존 테스트 | 상태 |
|---|---|
| `tests.test_agents.test_test_agent_cannot_change_production` | PASS |
| `tests.test_agents.test_developer_patch` | PASS |
| `tests.test_agents.test_symlink_patch` | PASS |
| `tests.test_agents.test_review_prompt_is_independent` | PASS |
| `tests.test_agents.test_junit_requires_executed_tests` | PASS |
| `tests.test_agents.test_miscounted_patch_is_recounted` | PASS |
| `tests.test_agents.test_existing_tests_are_protected` | PASS |
| `tests.test_bootstrap.test_supported_runtime` | PASS |
| `tests.test_cli.test_cli_help` | PASS |
| `tests.test_cli.test_cli_queries` | PASS |
| `tests.test_codex.test_strict_model_schema_has_no_open_dicts` | PASS |
| `tests.test_codex.test_codex_process_contract[True]` | PASS |
| `tests.test_codex.test_codex_process_contract[False]` | PASS |
| `tests.test_engine.test_config` | PASS |
| `tests.test_engine.test_unsafe_paths[../x]` | PASS |
| `tests.test_engine.test_unsafe_paths[/etc/passwd]` | PASS |
| `tests.test_engine.test_unsafe_paths[.git/config]` | PASS |
| `tests.test_engine.test_unsafe_paths[tests/../../x]` | PASS |
| `tests.test_engine.test_unsafe_paths[.]` | PASS |
| `tests.test_engine.test_store` | PASS |
| `tests.test_engine.test_process` | PASS |
| `tests.test_security.test_root_alias_is_allowed` | PASS |
| `tests.test_security.test_test_command_id_cannot_traverse` | PASS |
| `tests.test_security.test_execution_lock_is_exclusive` | PASS |
| `tests.test_security.test_process_cancel` | PASS |
| `tests.test_security.test_output_limit` | PASS |
| `tests.test_security.test_actual_sandbox_denies_source_writes_and_network` | PASS |
| `tests.test_workflow.test_success_and_approval` | PASS |
| `tests.test_workflow.test_fix_loop` | PASS |
| `tests.test_workflow.test_budget_exhaustion` | PASS |
| `tests.test_workflow.test_resume_preserves_completed_steps` | PASS |
| `tests.test_workflow.test_approval_rejects_changed_head` | PASS |
| `tests.test_workflow.test_cancellation` | PASS |
| `tests.test_workflow.test_tampered_evidence_blocks_approval` | PASS |
| `tests.test_workflow.test_crashed_active_run_can_resume_when_lock_is_free` | PASS |
| `tests.test_workflow.test_completed_worker_cannot_revert_approval` | PASS |
| `tests.test_workflow.test_test_failure_causes_fix_loop` | PASS |
| `tests.test_workflow.test_ambiguous_plan_needs_input` | PASS |

## 품질 항목별 상세 결과

<a id="a01"></a>
### A01 — CLI 기본 실행·조회·승인 및 알 수 없는 실행 ID 오류

- 중요도: **필수**
- 상태: **PASS**
- 검증 방법: Typer CliRunner + 동기 Mock 실행기. run/status/list/logs/report/approve와 잘못된 ID 실행. start만 Mock이며 이 항목은 실제 Worker 검증을 뜻하지 않음.
- 실행한 명령어: `CMD-AUDIT`
- 관련 파일 및 코드 위치: [src/dev_orchestra/cli.py:16](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/cli.py#L16), [src/dev_orchestra/application/service.py:22](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/application/service.py#L22)
- 실패 원인: 없음. 이 항목에서 실제로 검증한 범위에 한한 통과입니다.
- 필요한 수정 사항: 필요 없음. 단, 별도 실패 항목 및 미검증 범위는 그대로 남습니다.
- 연결된 결함: 없음

실제 결과:

```text
{
  "run": 0,
  "status": 0,
  "list": 0,
  "logs": 0,
  "report": 0,
  "approve": 0,
  "unknown-status": 1
}
```

<a id="a02"></a>
### A02 — PRD→계획→개발→테스트 생성·실행→리뷰→승인

- 중요도: **필수**
- 상태: **PASS**
- 검증 방법: 임시 Git 저장소, Mock 역할 에이전트, 실제 macOS 샌드박스 pytest. 원본에 미커밋 변경을 둠.
- 실행한 명령어: `CMD-AUDIT`
- 관련 파일 및 코드 위치: [src/dev_orchestra/orchestration/runner.py:64](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/orchestration/runner.py#L64), [src/dev_orchestra/application/service.py:126](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/application/service.py#L126)
- 실패 원인: 없음. 이 항목에서 실제로 검증한 범위에 한한 통과입니다.
- 필요한 수정 사항: 필요 없음. 단, 별도 실패 항목 및 미검증 범위는 그대로 남습니다.
- 연결된 결함: 없음

실제 결과:

```text
{
  "state": "waiting_approval",
  "calls": [
    "planner",
    "developer",
    "tester",
    "reviewer"
  ],
  "tests": {
    "passed": true,
    "commands": [
      {
        "command_id": "unit",
        "argv": [
          "/Users/hycho-office/dev/workspace/tools/dev-orchestra/.venv/bin/python",
          "-m",
          "pytest",
          "tests",
          "--junitxml=build/junit.xml",
          "-q"
        ],
        "exit_code": 0,
        "total": 1,
        "failed": 0,
        "skipped": 0,
        "failures": [],
        "passed": true
      }
    ]
  },
  "iteration": 0,
  "original_unchanged": true,
  "approved_state": "completed"
}
```

<a id="a03"></a>
### A03 — 실제 테스트 실패→Fix→테스트·리뷰 재실행

- 중요도: **필수**
- 상태: **PASS**
- 검증 방법: 처음에는 value=0으로 pytest 실패. Mock Fix가 value=1 패치를 생성. 재테스트·재리뷰 수행.
- 실행한 명령어: `CMD-AUDIT`
- 관련 파일 및 코드 위치: [src/dev_orchestra/orchestration/runner.py:179](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/orchestration/runner.py#L179), [src/dev_orchestra/orchestration/runner.py:231](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/orchestration/runner.py#L231)
- 실패 원인: 없음. 이 항목에서 실제로 검증한 범위에 한한 통과입니다.
- 필요한 수정 사항: 필요 없음. 단, 별도 실패 항목 및 미검증 범위는 그대로 남습니다.
- 연결된 결함: 없음

실제 결과:

```text
{
  "state": "waiting_approval",
  "calls": [
    "planner",
    "developer",
    "tester",
    "reviewer",
    "fixer",
    "tester",
    "reviewer"
  ],
  "tests": {
    "passed": true,
    "commands": [
      {
        "command_id": "unit",
        "argv": [
          "/Users/hycho-office/dev/workspace/tools/dev-orchestra/.venv/bin/python",
          "-m",
          "pytest",
          "tests",
          "--junitxml=build/junit.xml",
          "-q"
        ],
        "exit_code": 0,
        "total": 1,
        "failed": 0,
        "skipped": 0,
        "failures": [],
        "passed": true
      }
    ]
  },
  "iteration": 1,
  "original_unchanged": true,
  "approved_state": "completed"
}
```

<a id="a04"></a>
### A04 — 자동 수정 한도 0회

- 중요도: **필수**
- 상태: **PASS**
- 검증 방법: High 리뷰 문제를 반환하고 최대 수정 횟수를 0으로 설정.
- 실행한 명령어: `CMD-AUDIT`
- 관련 파일 및 코드 위치: [src/dev_orchestra/orchestration/runner.py:238](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/orchestration/runner.py#L238)
- 실패 원인: 없음. 이 항목에서 실제로 검증한 범위에 한한 통과입니다.
- 필요한 수정 사항: 필요 없음. 단, 별도 실패 항목 및 미검증 범위는 그대로 남습니다.
- 연결된 결함: 없음

실제 결과:

```text
{
  "state": "needs_input",
  "calls": [
    "planner",
    "developer",
    "tester",
    "reviewer"
  ]
}
```

<a id="a05"></a>
### A05 — Critical/High 차단 및 Medium/Low 기본 비차단

- 중요도: **필수**
- 상태: **PASS**
- 검증 방법: 같은 임시 코드 위치에 각 심각도 문제를 반환하고 상태를 비교.
- 실행한 명령어: `CMD-AUDIT`
- 관련 파일 및 코드 위치: [src/dev_orchestra/config.py:31](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/config.py#L31), [src/dev_orchestra/orchestration/runner.py:231](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/orchestration/runner.py#L231)
- 실패 원인: 없음. 이 항목에서 실제로 검증한 범위에 한한 통과입니다.
- 필요한 수정 사항: 필요 없음. 단, 별도 실패 항목 및 미검증 범위는 그대로 남습니다.
- 연결된 결함: 없음

실제 결과:

```text
{
  "critical": "needs_input",
  "high": "needs_input",
  "medium": "waiting_approval",
  "low": "waiting_approval"
}
```

<a id="b01"></a>
### B01 — 잘못된 JSON·인증 실패·실행 타임아웃 처리

- 중요도: **필수**
- 상태: **PASS**
- 검증 방법: Codex 대신 실행 가능한 스텁을 사용. 비정상 JSON, 인증 실패 종료 코드 1, 실행 시간 초과를 각각 발생. 실제 서버 인증 실패 검증은 N03으로 분리.
- 실행한 명령어: `CMD-AUDIT`
- 관련 파일 및 코드 위치: [src/dev_orchestra/infrastructure/codex.py:61](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/infrastructure/codex.py#L61), [src/dev_orchestra/infrastructure/process.py:65](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/infrastructure/process.py#L65)
- 실패 원인: 없음. 이 항목에서 실제로 검증한 범위에 한한 통과입니다.
- 필요한 수정 사항: 필요 없음. 단, 별도 실패 항목 및 미검증 범위는 그대로 남습니다.
- 연결된 결함: 없음

실제 결과:

```text
{
  "bad-json": "invalid Codex result schema",
  "auth": "Codex reviewer exited with code 1",
  "timeout": "process timeout"
}
```

<a id="b02"></a>
### B02 — Codex 사전 기능 확인의 시간 제한·취소 가능성

- 중요도: **필수**
- 상태: **FAIL**
- 검증 방법: --help에서 멈추는 Mock Codex로 timeout_seconds=1을 설정. 1.5초 뒤 프로세스 생존을 확인하고 검증 프로세스 그룹을 정리.
- 실행한 명령어: `CMD-AUDIT`
- 관련 파일 및 코드 위치: [src/dev_orchestra/infrastructure/codex.py:26](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/infrastructure/codex.py#L26), [src/dev_orchestra/worker.py:19](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/worker.py#L19)
- 실패 원인: CodexClient.check의 subprocess.run에는 timeout이 없고 Worker가 실행 잠금 및 제어 확인 전에 호출한다. 실행 타임아웃을 1초로 설정해도 --help 프로세스가 계속 대기했다.
- 필요한 수정 사항: 사전 점검·Git 작업을 포함한 모든 외부 호출에 제한 시간과 취소 제어를 적용한다. Worker 소유권과 상태 검증을 사전 점검보다 먼저 확보한다.
- 연결된 결함: Q06

실제 결과:

```text
{
  "still_running_after_seconds": 1.5,
  "timeout_configured_seconds": 1,
  "alive": true
}
```

<a id="b03"></a>
### B03 — JSON·Bearer 형식 로그의 민감값 마스킹

- 중요도: **필수**
- 상태: **FAIL**
- 검증 방법: 실제 비밀값 대신 합성 canary를 JSON token 및 Authorization Bearer에 넣어 execute 출력 로그 검사.
- 실행한 명령어: `CMD-AUDIT`
- 관련 파일 및 코드 위치: [src/dev_orchestra/infrastructure/process.py:18](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/infrastructure/process.py#L18), [src/dev_orchestra/infrastructure/process.py:53](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/infrastructure/process.py#L53)
- 실패 원인: 정규식은 키 다음의 따옴표를 처리하지 못하고 일반 Bearer 헤더도 다루지 않는다. 알려진 sk-/PAT 접두사 마스킹과 별개로 합성 민감값이 로그에 저장됐다.
- 필요한 수정 사항: 구조화 로그에서 민감 필드를 먼저 제거하고 JSON·헤더·텍스트 로그를 일관되게 처리한다. 알고 있는 환경 비밀값과 분할 출력도 검증하고 response/result/report 저장 경로도 점검한다.
- 연결된 결함: Q05

실제 결과:

```text
{
  "exit_code": 0,
  "synthetic_secret_persisted": true,
  "format": "JSON token / Authorization Bearer"
}
```

<a id="b04"></a>
### B04 — 타임아웃 후 자식 프로세스 전체 종료

- 중요도: **필수**
- 상태: **FAIL**
- 검증 방법: 자식은 SIGTERM을 무시하고 부모는 기본 종료하도록 구성. 시간 초과 후 자식 생존과 finished 마커 검사. 검증 후 남은 자식 PID는 직접 종료.
- 실행한 명령어: `CMD-AUDIT`
- 관련 파일 및 코드 위치: [src/dev_orchestra/infrastructure/process.py:91](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/infrastructure/process.py#L91), [src/dev_orchestra/application/service.py:88](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/application/service.py#L88)
- 실패 원인: SIGTERM 후 부모 wait가 빨리 반환하면 프로세스 그룹의 잔여 자식에 SIGKILL을 보내지 않는다. 그 상태에서도 marker.finished=true가 기록된다.
- 필요한 수정 사항: 부모 종료와 그룹 전체 종료를 분리해 추적하고 유예 시간 후 잔여 그룹을 종료한다. 프로세스 식별·소유권과 실제 종료 확인을 기록해 resume/cancel에 사용한다.
- 연결된 결함: Q02

실제 결과:

```text
{
  "error": "process timeout",
  "descendant_alive_after_timeout": true,
  "marker_finished": true,
  "cleanup": "검증용 자식 PID를 종료함"
}
```

<a id="c01"></a>
### C01 — 최종 승인 대상과 실제 테스트·리뷰 SHA 일치

- 중요도: **필수**
- 상태: **FAIL**
- 검증 방법: 실제 pytest 통과 후 reporting 시점에 외부 커밋을 삽입하는 계측 Runner 사용. 제품의 report/approve 구현은 그대로 호출. 승인 후 실제 pytest를 재실행.
- 실행한 명령어: `CMD-STALE`
- 관련 파일 및 코드 위치: [src/dev_orchestra/orchestration/runner.py:99](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/orchestration/runner.py#L99), [src/dev_orchestra/application/service.py:126](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/application/service.py#L126)
- 실패 원인: 완료 경로는 현재 HEAD를 final_sha로 저장하며 test_result.sha/review_result.sha와 대조하지 않는다. approve도 해당 비교가 없다. Worktree가 없으면 status는 Feature ref 변경을 검사하지 않는다.
- 필요한 수정 사항: 검증 증거를 동일한 후보 SHA·정책·입력 해시에 연결하고 보고서 생성·승인 직전에 재확인한다. 달라지면 검증을 무효화하고 Test/Review부터 재실행한다. cleanup 이후에는 Feature ref와 과거 승인 SHA를 구분해 표시한다.
- 연결된 결함: Q01

실제 결과:

```text
{
  "approved_state": "completed",
  "old_test_passed": true,
  "test_sha": "fd12377f09e0d1572a48b01ad3d4a295bac48a9a",
  "final_sha": "7e114fda44f419974a565a6eaf72065878b78f93",
  "actual_retest_passed": false
}
```

<a id="c02"></a>
### C02 — 호스트 Git 작업의 저장소 실행 코드 격리

- 중요도: **필수**
- 상태: **FAIL**
- 검증 방법: 임시 저장소에 clean filter driver를 설정하고 .gitattributes가 이를 참조하도록 구성. Git.commit이 임시 저장소 외부 canary 파일을 쓰는지 확인. 실제 사용자 파일·네트워크는 사용하지 않음.
- 실행한 명령어: `CMD-AUDIT`
- 관련 파일 및 코드 위치: [src/dev_orchestra/infrastructure/git.py:14](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/infrastructure/git.py#L14), [src/dev_orchestra/infrastructure/git.py:56](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/infrastructure/git.py#L56)
- 실패 원인: Git 호출은 hooks·서명·fsmonitor만 비활성화한다. 저장소에 설정된 clean filter는 git add에서 그대로 실행된다. 조건은 해당 filter driver가 저장소 설정에 존재하는 경우다. PRD만으로 임의 드라이버를 새로 설정할 수 있다고 입증한 것은 아니다.
- 필요한 수정 사항: 저장소 filter·외부 프로그램·환경 설정의 신뢰 경계를 정의한다. 호스트 Git도 파일·네트워크 제한 하에서 실행하거나 미승인 filter 사용을 사전 거절한다. 필요한 LFS 등은 명시적 허용 정책으로 분리한다.
- 연결된 결함: Q03

실제 결과:

```text
{
  "unsandboxed_clean_filter_executed": true,
  "write_location": "임시 저장소 외부의 검증용 파일",
  "precondition": "저장소에 clean filter driver 설정이 존재함"
}
```

<a id="c03"></a>
### C03 — Feature 브랜치 이름 충돌의 안전한 실패

- 중요도: **필수**
- 상태: **PASS**
- 검증 방법: 실행에 예약된 Feature 브랜치를 미리 만든 뒤 Runner 실행. 원본 SHA와 Worktree 생성 여부 검사.
- 실행한 명령어: `CMD-AUDIT`
- 관련 파일 및 코드 위치: [src/dev_orchestra/infrastructure/git.py:46](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/infrastructure/git.py#L46), [src/dev_orchestra/orchestration/runner.py:72](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/orchestration/runner.py#L72)
- 실패 원인: 없음. 이 항목에서 실제로 검증한 범위에 한한 통과입니다.
- 필요한 수정 사항: 필요 없음. 단, 별도 실패 항목 및 미검증 범위는 그대로 남습니다.
- 연결된 결함: 없음

실제 결과:

```text
{
  "state": "failed",
  "original_sha_preserved": true,
  "error": "git worktree failed: Preparing worktree (new branch 'feature/ai/run-834e0312aea3463b')\nfatal: a branch named 'feature/ai/run-834e0312aea3463b' already exists"
}
```

<a id="d01"></a>
### D01 — 기존 테스트 무력화 패치 차단

- 중요도: **필수**
- 상태: **FAIL**
- 검증 방법: 기존 assert False 테스트의 실제 실패를 확인한 뒤 tester 역할로 assert True 변경 패치 적용. 같은 명령으로 실제 pytest 재실행.
- 실행한 명령어: `CMD-AUDIT`
- 관련 파일 및 코드 위치: [src/dev_orchestra/agents/policy.py:52](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/agents/policy.py#L52), [src/dev_orchestra/orchestration/runner.py:173](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/orchestration/runner.py#L173)
- 실패 원인: tester 역할에는 경로만 제한하고 기존 테스트 내용 변경은 허용한다. assert False를 assert True로 바꾸는 패치가 적용돼 실제 실패가 성공으로 바뀌었다. Fix 뒤에도 Test Agent가 다시 실행되므로 Fix의 테스트 보호만으로 충분하지 않다.
- 필요한 수정 사항: 기존 테스트의 삭제·약화·skip/xfail 추가를 호스트 정책으로 제한하고, 허용 변경은 명시적 검토로 전환한다. 테스트 기준 스냅샷을 고정하고 독립 리뷰 증거를 남긴다.
- 연결된 결함: Q04

실제 결과:

```text
{
  "before_passed": false,
  "after_passed": true,
  "test_agent_patch_accepted": true,
  "production_unchanged": true
}
```

<a id="d02"></a>
### D02 — JUnit 상위 testsuite 오류 검출

- 중요도: **필수**
- 상태: **FAIL**
- 검증 방법: testsuite errors=1, 하위 testcase에는 오류가 없는 보고서를 파싱. 실패로 취급하는지 검사.
- 실행한 명령어: `CMD-AUDIT`
- 관련 파일 및 코드 위치: [src/dev_orchestra/infrastructure/test_runner.py:53](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/infrastructure/test_runner.py#L53)
- 실패 원인: testcase의 failure/error만 집계하고 testsuite의 errors/failures 및 집계 일관성을 검사하지 않는다. 상위 errors=1 보고서가 failed=0으로 해석됐다.
- 필요한 수정 사항: 지원하는 JUnit 방언을 정하고 suite/case 집계 불일치와 전역 오류를 실행 실패 또는 파싱 실패로 처리한다.
- 연결된 결함: Q10

실제 결과:

```text
{
  "declared_suite_errors": 1,
  "parsed": {
    "total": 1,
    "failed": 0,
    "skipped": 0,
    "failures": []
  }
}
```

<a id="e01"></a>
### E01 — 실제 Worker 강제 종료 후 완료 단계 재사용

- 중요도: **필수**
- 상태: **PASS**
- 검증 방법: Mock 에이전트를 사용하는 별도 Python Runner를 developing에서 SIGKILL. OS 잠금 해제 후 resume. Planner 재호출 여부 검사. 부분 패치·커밋 경계 전체 검증은 N04로 분리.
- 실행한 명령어: `CMD-AUDIT`
- 관련 파일 및 코드 위치: [src/dev_orchestra/application/service.py:80](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/application/service.py#L80), [src/dev_orchestra/orchestration/runner.py:109](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/orchestration/runner.py#L109)
- 실패 원인: 없음. 이 항목에서 실제로 검증한 범위에 한한 통과입니다.
- 필요한 수정 사항: 필요 없음. 단, 별도 실패 항목 및 미검증 범위는 그대로 남습니다.
- 연결된 결함: 없음

실제 결과:

```text
{
  "state_after_sigkill": "developing",
  "resumed_calls": [
    "developer",
    "tester",
    "reviewer"
  ],
  "final_state": "waiting_approval"
}
```

<a id="e02"></a>
### E02 — 다른 실행의 잠금 대기 중 취소 반영

- 중요도: **필수**
- 상태: **FAIL**
- 검증 방법: DB Worker 잠금을 별도 소유한 채 대기 Worker를 실행하고 cancel 요청. 잠금 유지 중 상태와 생존을 검사한 뒤 해제.
- 실행한 명령어: `CMD-AUDIT`
- 관련 파일 및 코드 위치: [src/dev_orchestra/infrastructure/locking.py:16](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/infrastructure/locking.py#L16), [src/dev_orchestra/orchestration/runner.py:40](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/orchestration/runner.py#L40)
- 실패 원인: 전역 DB·저장소 flock을 블로킹으로 대기하며 제어 요청을 확인하지 않는다. 잠금 대기 자체는 실행 타임아웃에 포함되지 않는다.
- 필요한 수정 사항: 취소 가능한 대기·작업 claim/lease 구조를 도입하고 큐 작업 취소는 실행 슬롯과 무관하게 확정할 수 있게 한다.
- 연결된 결함: Q08

실제 결과:

```text
{
  "while_other_run_holds_lock": {
    "state": "queued",
    "control": "cancel",
    "worker_alive": true
  },
  "after_releasing_other_run": "cancelled"
}
```

<a id="e03"></a>
### E03 — 상태 전이 및 DB 스키마 버전 검증

- 중요도: **필수**
- 상태: **FAIL**
- 검증 방법: Store.update로 정의되지 않은 상태를 입력. schema_version=999인 임시 DB를 Store로 열어 거절 여부 검사. 두 세부 검사가 모두 실패.
- 실행한 명령어: `CMD-AUDIT`
- 관련 파일 및 코드 위치: [src/dev_orchestra/infrastructure/store.py:17](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/infrastructure/store.py#L17), [src/dev_orchestra/infrastructure/store.py:78](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/infrastructure/store.py#L78), [src/dev_orchestra/domain/models.py:14](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/domain/models.py#L14)
- 실패 원인: State enum은 Store 갱신을 제한하지 않고 임의 상태 문자열이 수용된다. schema_version은 버전 1을 삽입할 뿐 지원 여부나 마이그레이션을 검사하지 않아 999와 1이 공존했다.
- 필요한 수정 사항: 합법적 전이표·예상 상태/버전 조건 갱신을 서비스와 저장소 경계에 적용한다. 단일 현재 버전과 순차 마이그레이션, 미지원 미래 버전 거절 정책을 마련한다.
- 연결된 결함: Q09

실제 결과:

```text
{
  "accepted_state": "invented_state",
  "schema_versions": [
    1,
    999
  ],
  "future_schema_rejected": false
}
```

<a id="f01"></a>
### F01 — 승인 후 최종 보고서의 상태 일치

- 중요도: **필수**
- 상태: **FAIL**
- 검증 방법: 정상 워크플로 승인 후 DB 상태와 report 명령이 읽는 final-report.md 내용 비교.
- 실행한 명령어: `CMD-AUDIT`
- 관련 파일 및 코드 위치: [src/dev_orchestra/application/service.py:126](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/application/service.py#L126), [src/dev_orchestra/application/service.py:174](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/application/service.py#L174), [src/dev_orchestra/orchestration/runner.py:245](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/orchestration/runner.py#L245)
- 실패 원인: approve는 DB만 변경하고 report는 승인 이전 final-report.md를 그대로 반환한다.
- 필요한 수정 사항: 승인·취소·무효화 후 보고서/metadata를 다시 생성하거나 조회 시 DB 기준 승인 정보를 합성한다.
- 연결된 결함: Q11

실제 결과:

```text
{
  "database_state": "completed",
  "report_still_waiting_approval": true
}
```

<a id="f02"></a>
### F02 — 잘못된 YAML 설정의 사용자 오류 처리

- 중요도: **필수**
- 상태: **FAIL**
- 검증 방법: project: [ 로 끝나는 잘못된 YAML을 전달해 CliRunner 종료 코드·출력·예외 유형 확인.
- 실행한 명령어: `CMD-AUDIT`
- 관련 파일 및 코드 위치: [src/dev_orchestra/cli.py:16](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/cli.py#L16), [src/dev_orchestra/config.py:89](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/config.py#L89)
- 실패 원인: guarded가 yaml.YAMLError를 잡지 않아 CliRunner에서는 ParserError와 빈 출력이 발생했다. 실제 터미널에서는 상위 실행 환경에 따라 traceback이 노출될 수 있다.
- 필요한 수정 사항: 설정 로더 오류를 도메인 입력 오류로 정규화하고 민감한 설정 원문을 노출하지 않는 메시지와 종료 코드를 제공한다.
- 연결된 결함: Q12

실제 결과:

```text
{
  "exit_code": 1,
  "output": "",
  "exception_type": "ParserError"
}
```

<a id="f03"></a>
### F03 — Worker 사전 실패의 보고서 생성

- 중요도: **권장**
- 상태: **FAIL**
- 검증 방법: 존재하지 않는 Codex 실행 파일을 설정하고 실제 별도 Worker 실행. failed 상태 및 final-report.md 존재 검사.
- 실행한 명령어: `CMD-AUDIT`
- 관련 파일 및 코드 위치: [src/dev_orchestra/worker.py:19](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/worker.py#L19), [src/dev_orchestra/application/service.py:65](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/application/service.py#L65)
- 실패 원인: worker.main의 예외 경로는 Store만 failed로 갱신하며 Runner.report를 호출하지 않는다.
- 필요한 수정 사항: 사전 실패를 포함해 모든 실행 종료 경로에 공통 실패 결과·보고서 생성을 적용한다.
- 연결된 결함: Q13

실제 결과:

```text
{
  "state": "failed",
  "error": "[Errno 2] No such file or directory: '/private/tmp/devorchestra-readiness-20261102-hrFzNO/does-not-exist'",
  "report_exists": false
}
```

<a id="f04"></a>
### F04 — Worktree 정리 후 승인 유효성 재확인

- 중요도: **필수**
- 상태: **FAIL**
- 검증 방법: 정상 승인→cleanup 후 임시 저장소의 Feature ref를 새 커밋으로 이동. status의 현재 결과 상태 확인. 과거 승인 이력의 SHA는 유지돼야 하지만 현재 결과의 유효성과 구분되어야 함.
- 실행한 명령어: `CMD-AUDIT`
- 관련 파일 및 코드 위치: [src/dev_orchestra/application/service.py:151](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/application/service.py#L151), [src/dev_orchestra/application/service.py:159](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/application/service.py#L159)
- 실패 원인: 완료 경로는 현재 HEAD를 final_sha로 저장하며 test_result.sha/review_result.sha와 대조하지 않는다. approve도 해당 비교가 없다. Worktree가 없으면 status는 Feature ref 변경을 검사하지 않는다.
- 필요한 수정 사항: 검증 증거를 동일한 후보 SHA·정책·입력 해시에 연결하고 보고서 생성·승인 직전에 재확인한다. 달라지면 검증을 무효화하고 Test/Review부터 재실행한다. cleanup 이후에는 Feature ref와 과거 승인 SHA를 구분해 표시한다.
- 연결된 결함: Q01

실제 결과:

```text
{
  "status_after_feature_ref_changed": "completed",
  "feature_sha": "1cad495587c07e2ffc2dd9220801fb48abd1849f",
  "approved_sha": "0ec758f37cb68d89a5dea41771c54112e4c6a000",
  "worktree_exists": false
}
```

<a id="f05"></a>
### F05 — 제어 요청의 영속 이벤트 기록

- 중요도: **권장**
- 상태: **FAIL**
- 검증 방법: cancel 제어 요청 전후 events 커서 조회. 완료 상태 이벤트와 별개로 요청 이벤트가 남는지 검사.
- 실행한 명령어: `CMD-AUDIT`
- 관련 파일 및 코드 위치: [src/dev_orchestra/infrastructure/store.py:95](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/infrastructure/store.py#L95), [src/dev_orchestra/infrastructure/store.py:131](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/infrastructure/store.py#L131)
- 실패 원인: control 갱신은 이벤트를 쓰지 않는다. 저장된 이벤트는 주로 state 문자열이고 step_id/iteration/로그 커서가 없다.
- 필요한 수정 사항: 명령 요청·수락·종료, 단계·회차·로그 참조를 버전 있는 이벤트로 저장한다. GUI의 SSE는 이를 커서로 조회하는 어댑터로 구현한다.
- 연결된 결함: Q14

실제 결과:

```text
{
  "events_before": 0,
  "events_after_cancel_request": 0,
  "control": "cancel"
}
```

<a id="g01"></a>
### G01 — 누락 필드가 있는 리뷰 JSON의 거절

- 중요도: **필수**
- 상태: **FAIL**
- 검증 방법: Mock Codex가 summary만 있는 JSON 반환. 출력 JSON Schema의 required 목록과 host Pydantic 검증 결과 비교.
- 실행한 명령어: `CMD-EXTRA`
- 관련 파일 및 코드 위치: [src/dev_orchestra/domain/models.py:53](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/domain/models.py#L53), [src/dev_orchestra/infrastructure/codex.py:11](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/infrastructure/codex.py#L11), [src/dev_orchestra/infrastructure/codex.py:73](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/infrastructure/codex.py#L73)
- 실패 원인: 출력 JSON Schema에서는 모든 필드를 required로 만들지만 host AgentResult에는 기본값이 있다. summary만 있는 응답도 issues=[]로 채워진다. CLI/provider의 스키마 제약이 항상 성공한다고 가정하는 경계다.
- 필요한 수정 사항: 역할별 필수 필드와 완료 상태를 명시하고 실제 수신 JSON을 동일한 필수 스키마로 검증한다. 리뷰 필드 누락·실행 미완료·거절 응답은 통과 판정과 구분한다.
- 연결된 결함: Q07

실제 결과:

```text
{
  "accepted_incomplete_json": true,
  "defaulted_issues": [],
  "supplied_fields": [
    "summary"
  ]
}
```

<a id="g02"></a>
### G02 — 기준 커밋 고정과 공통 조상 기반 리뷰 Diff

- 중요도: **필수**
- 상태: **PASS**
- 검증 방법: 실행 생성 이후 원본 main에 커밋 추가. Mock Reviewer에 전달되는 기준 SHA와 실제 Git Diff 확인.
- 실행한 명령어: `CMD-EXTRA`
- 관련 파일 및 코드 위치: [src/dev_orchestra/application/service.py:34](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/application/service.py#L34), [src/dev_orchestra/infrastructure/git.py:62](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/infrastructure/git.py#L62), [src/dev_orchestra/orchestration/runner.py:136](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/orchestration/runner.py#L136)
- 실패 원인: 없음. 이 항목에서 실제로 검증한 범위에 한한 통과입니다.
- 필요한 수정 사항: 필요 없음. 단, 별도 실패 항목 및 미검증 범위는 그대로 남습니다.
- 연결된 결함: 없음

실제 결과:

```text
{
  "recorded_base_sha": "55786ff3b2c42c3a70e8720d3f004bd710fb0023",
  "moved_branch_sha": "e6bf00ed3d1015a8a1df36442d9d8218e55c97d3",
  "diff": "diff --git a/main.py b/main.py\nindex 533d1af..0a979d4 100644\n--- a/main.py\n+++ b/main.py\n@@ -1 +1 @@\n-value = 0\n+value = 1",
  "state": "waiting_approval"
}
```

<a id="g03"></a>
### G03 — 승인 시 자동 Push·Merge 미수행

- 중요도: **필수**
- 상태: **PASS**
- 검증 방법: GitHub 대신 로컬 bare 저장소를 origin으로 사용. 승인 전후 원격 refs 확인. 실제 네트워크·사용자 원격은 건드리지 않음. C02의 간접 실행 문제는 별도 FAIL.
- 실행한 명령어: `CMD-EXTRA`
- 관련 파일 및 코드 위치: [src/dev_orchestra/application/service.py:126](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/application/service.py#L126), [src/dev_orchestra/infrastructure/git.py:56](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/infrastructure/git.py#L56)
- 실패 원인: 없음. 이 항목에서 실제로 검증한 범위에 한한 통과입니다.
- 필요한 수정 사항: 필요 없음. 단, 별도 실패 항목 및 미검증 범위는 그대로 남습니다.
- 연결된 결함: 없음

실제 결과:

```text
{
  "local_bare_remote_refs_after_approve": "",
  "state": "completed"
}
```

<a id="g04"></a>
### G04 — 역할별 독립 Codex 프로세스와 실행 옵션

- 중요도: **필수**
- 상태: **PASS**
- 검증 방법: Mock Codex 실행 파일에서 PID·role·argv를 기록. 4개 역할의 PID가 다르고 read-only/ephemeral, resume 미사용인지 검사. 실제 Codex 권한 집행은 N02로 분리.
- 실행한 명령어: `CMD-EXTRA`
- 관련 파일 및 코드 위치: [src/dev_orchestra/infrastructure/codex.py:34](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/infrastructure/codex.py#L34)
- 실패 원인: 없음. 이 항목에서 실제로 검증한 범위에 한한 통과입니다.
- 필요한 수정 사항: 필요 없음. 단, 별도 실패 항목 및 미검증 범위는 그대로 남습니다.
- 연결된 결함: 없음

실제 결과:

```text
{
  "state": "waiting_approval",
  "roles": [
    "planner",
    "developer",
    "tester",
    "reviewer"
  ],
  "distinct_pids": 4,
  "fresh_read_only_flags_checked": true
}
```

<a id="g05"></a>
### G05 — 동일 실행의 동시 Worker 중복 실행 차단

- 중요도: **필수**
- 상태: **PASS**
- 검증 방법: 별도 프로세스가 planner에서 실행 잠금을 보유하는 동안 같은 run_id를 다시 실행. 호출 횟수와 중복 거절 확인.
- 실행한 명령어: `CMD-EXTRA`
- 관련 파일 및 코드 위치: [src/dev_orchestra/infrastructure/locking.py:16](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/infrastructure/locking.py#L16), [src/dev_orchestra/orchestration/runner.py:40](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/orchestration/runner.py#L40)
- 실패 원인: 없음. 이 항목에서 실제로 검증한 범위에 한한 통과입니다.
- 필요한 수정 사항: 필요 없음. 단, 별도 실패 항목 및 미검증 범위는 그대로 남습니다.
- 연결된 결함: 없음

실제 결과:

```text
{
  "duplicate_rejected": true,
  "calls": [
    "planner",
    "developer",
    "tester",
    "reviewer"
  ],
  "state": "waiting_approval"
}
```

<a id="g06"></a>
### G06 — 재개 전 선행 완료 산출물 검증

- 중요도: **필수**
- 상태: **FAIL**
- 검증 방법: reviewer에서 실패한 실행의 완료된 tester 산출물을 변조하고 resume. reviewer 재호출 전 변조 감지 여부 확인. 승인 단계에서는 변조가 차단되는지도 별도 확인.
- 실행한 명령어: `CMD-EXTRA`
- 관련 파일 및 코드 위치: [src/dev_orchestra/orchestration/runner.py:109](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/orchestration/runner.py#L109), [src/dev_orchestra/application/service.py:80](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/application/service.py#L80), [src/dev_orchestra/application/service.py:142](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/application/service.py#L142)
- 실패 원인: 다음 실행 역할에 해당하는 완료 단계만 검사하고 이전 tester 등 선행 증거는 approve까지 검증하지 않는다. 변조 후 reviewer가 다시 호출되고 waiting_approval에 도달했다. 승인은 정상적으로 차단됐다.
- 필요한 수정 사항: resume 시 전체 의존 체크포인트의 SHA·입력 해시·스키마·체크섬을 먼저 검증하고 유효성 손상 지점부터 재실행하거나 개입을 요청한다.
- 연결된 결함: Q15

실제 결과:

```text
{
  "resumed_calls": [
    "reviewer"
  ],
  "state_before_approval": "waiting_approval",
  "approval_blocked": true
}
```

<a id="g07"></a>
### G07 — SQLite 파일 및 외래 키 정합성

- 중요도: **필수**
- 상태: **PASS**
- 검증 방법: 주 재현·추가 재현 당시 생성된 임시 state.db 23개에 integrity_check와 foreign_key_check 수행. 이후 생성된 H 계열 DB까지 검사한 수치는 아님.
- 실행한 명령어: `CMD-EXTRA`
- 관련 파일 및 코드 위치: [src/dev_orchestra/infrastructure/store.py:41](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/infrastructure/store.py#L41)
- 실패 원인: 없음. 이 항목에서 실제로 검증한 범위에 한한 통과입니다.
- 필요한 수정 사항: 필요 없음. 단, 별도 실패 항목 및 미검증 범위는 그대로 남습니다.
- 연결된 결함: 없음

실제 결과:

```text
{
  "checked_databases": 23,
  "integrity_errors": []
}
```

<a id="g08"></a>
### G08 — 테스트 출력 경로에 추적 소스가 있을 때 차단

- 중요도: **필수**
- 상태: **PASS**
- 검증 방법: build/source.py를 Git 추적 상태로 만든 뒤 실제 TestRunner 실행.
- 실행한 명령어: `CMD-EXTRA`
- 관련 파일 및 코드 위치: [src/dev_orchestra/infrastructure/test_runner.py:96](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/infrastructure/test_runner.py#L96)
- 실패 원인: 없음. 이 항목에서 실제로 검증한 범위에 한한 통과입니다.
- 필요한 수정 사항: 필요 없음. 단, 별도 실패 항목 및 미검증 범위는 그대로 남습니다.
- 연결된 결함: 없음

실제 결과:

```text
{
  "blocked": true,
  "error": "output path contains tracked source files"
}
```

<a id="g09"></a>
### G09 — SQLite 재개방·WAL·외래 키·이벤트 커서

- 중요도: **필수**
- 상태: **PASS**
- 검증 방법: 상태를 저장하고 새 Store로 재개방. pragma와 after 커서 이후 이벤트 조회. 상태 전이 합법성은 E03에서 별도 판정.
- 실행한 명령어: `CMD-EXTRA`
- 관련 파일 및 코드 위치: [src/dev_orchestra/infrastructure/store.py:21](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/infrastructure/store.py#L21), [src/dev_orchestra/infrastructure/store.py:41](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/infrastructure/store.py#L41), [src/dev_orchestra/infrastructure/store.py:131](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/infrastructure/store.py#L131)
- 실패 원인: 없음. 이 항목에서 실제로 검증한 범위에 한한 통과입니다.
- 필요한 수정 사항: 필요 없음. 단, 별도 실패 항목 및 미검증 범위는 그대로 남습니다.
- 연결된 결함: 없음

실제 결과:

```text
{
  "reopened_value": "saved",
  "journal": "wal",
  "foreign_keys": 1,
  "events_after_cursor": 1
}
```

<a id="g10"></a>
### G10 — 테스트 0건·전체 skip·잘못된 XML 거절

- 중요도: **필수**
- 상태: **PASS**
- 검증 방법: 각 JUnit 입력을 실제 parser로 처리. testcase error도 실패로 집계하는지 검사.
- 실행한 명령어: `CMD-EXTRA`
- 관련 파일 및 코드 위치: [src/dev_orchestra/infrastructure/test_runner.py:53](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/infrastructure/test_runner.py#L53)
- 실패 원인: 없음. 이 항목에서 실제로 검증한 범위에 한한 통과입니다.
- 필요한 수정 사항: 필요 없음. 단, 별도 실패 항목 및 미검증 범위는 그대로 남습니다.
- 연결된 결함: 없음

실제 결과:

```text
{
  "empty": "rejected",
  "all-skipped": "rejected",
  "bad-xml": "rejected",
  "suite-error": {
    "total": 1,
    "failed": 1,
    "skipped": 0,
    "failures": [
      "unknown"
    ]
  }
}
```

<a id="g11"></a>
### G11 — 리뷰 입력에서 개발 추론·대화 분리

- 중요도: **필수**
- 상태: **PASS**
- 검증 방법: Reviewer 프롬프트 검사와 Runner의 context 구성 코드 대조. 개발 추론·대화 이력을 전달하는 필드가 없고 독립 검토 지시가 있는지 확인. 이는 프롬프트 인젝션 저항성 실증을 뜻하지 않음.
- 실행한 명령어: `CMD-EXTRA`
- 관련 파일 및 코드 위치: [src/dev_orchestra/agents/prompts.py:7](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/agents/prompts.py#L7), [src/dev_orchestra/orchestration/runner.py:127](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/orchestration/runner.py#L127)
- 실패 원인: 없음. 이 항목에서 실제로 검증한 범위에 한한 통과입니다.
- 필요한 수정 사항: 필요 없음. 단, 별도 실패 항목 및 미검증 범위는 그대로 남습니다.
- 연결된 결함: 없음

실제 결과:

```text
{
  "review_prompt_independent": true,
  "private_reasoning_prohibited": true,
  "developer_transcript_context": false
}
```

<a id="h01"></a>
### H01 — 실제 분리 Worker에서 CLI 취소 처리

- 중요도: **필수**
- 상태: **PASS**
- 검증 방법: Codex만 실행 파일 스텁으로 교체. CLI run --detach로 실제 Worker를 시작하고 developing 중 CLI cancel 호출. cancelled 및 프로세스 종료 마커 확인.
- 실행한 명령어: `CMD-FINAL`
- 관련 파일 및 코드 위치: [src/dev_orchestra/application/service.py:65](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/application/service.py#L65), [src/dev_orchestra/application/service.py:115](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/application/service.py#L115), [src/dev_orchestra/infrastructure/process.py:65](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/infrastructure/process.py#L65)
- 실패 원인: 없음. 이 항목에서 실제로 검증한 범위에 한한 통과입니다.
- 필요한 수정 사항: 필요 없음. 단, 별도 실패 항목 및 미검증 범위는 그대로 남습니다.
- 연결된 결함: 없음

실제 결과:

```text
{
  "run_exit": 0,
  "cancel_exit": 0,
  "state": "cancelled",
  "process_marker": {
    "pid": 53130,
    "finished": true
  }
}
```

<a id="h02"></a>
### H02 — 패치 경로 탈출·Git 메타데이터·프로덕션 변경 차단

- 중요도: **필수**
- 상태: **PASS**
- 검증 방법: ../escape, .git/config 및 tester의 main.py 수정 패치가 거절되는지 실제 Git 저장소로 확인.
- 실행한 명령어: `CMD-FINAL`
- 관련 파일 및 코드 위치: [src/dev_orchestra/agents/policy.py:39](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/agents/policy.py#L39), [src/dev_orchestra/infrastructure/files.py:15](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/infrastructure/files.py#L15)
- 실패 원인: 없음. 이 항목에서 실제로 검증한 범위에 한한 통과입니다.
- 필요한 수정 사항: 필요 없음. 단, 별도 실패 항목 및 미검증 범위는 그대로 남습니다.
- 연결된 결함: 없음

실제 결과:

```text
{
  "blocked": [
    "../escape",
    ".git/config",
    "main.py"
  ],
  "original_source": "value = 0\n"
}
```

<a id="h03"></a>
### H03 — 기본 3회 수정 제한과 회차별 산출물 보존

- 중요도: **필수**
- 상태: **PASS**
- 검증 방법: 계속 High 문제를 반환하는 Mock Reviewer로 전체 반복 수행. Fix 횟수, Test/Review 회차, 산출물 경로의 고유성 확인.
- 실행한 명령어: `CMD-FINAL`
- 관련 파일 및 코드 위치: [src/dev_orchestra/orchestration/runner.py:123](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/orchestration/runner.py#L123), [src/dev_orchestra/orchestration/runner.py:238](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/orchestration/runner.py#L238)
- 실패 원인: 없음. 이 항목에서 실제로 검증한 범위에 한한 통과입니다.
- 필요한 수정 사항: 필요 없음. 단, 별도 실패 항목 및 미검증 범위는 그대로 남습니다.
- 연결된 결함: 없음

실제 결과:

```text
{
  "state": "needs_input",
  "fix_count": 3,
  "test_iterations": [
    0,
    1,
    2,
    3
  ],
  "review_iterations": [
    0,
    1,
    2,
    3
  ],
  "unique_artifacts": 13
}
```

<a id="n01"></a>
### N01 — 현 버전 실제 Codex 전체 워크플로·Fix 품질

- 중요도: **필수**
- 상태: **NOT_TESTED**
- 검증 방법: 실제 모델 호출은 수행하지 않음. 이전 0.165.0 검증 기록을 현 버전 통과로 재사용하지 않음.
- 실행한 명령어: `실행하지 않음: python scripts/e2e.py`
- 관련 파일 및 코드 위치: [scripts/e2e.py:22](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/scripts/e2e.py#L22)
- 실패 원인: 실행하지 않았거나 현재 단계의 적용 범위 밖입니다. PASS로 해석하지 않습니다.
- 필요한 수정 사항: 차단 결함 수정 후 별도 검증 계획과 실행 환경·인증·사용량 조건을 확인해 수행합니다.
- 연결된 결함: 없음

실제 결과:

```text
0.166.0에서의 실제 PRD 해석·수정·리뷰 품질과 계정 사용 한도·요금 조건은 이번 검증에서 확인하지 않음.
```

<a id="n02"></a>
### N02 — 현 버전 실제 Codex 역할별 파일·네트워크 제한 집행

- 중요도: **필수**
- 상태: **NOT_TESTED**
- 검증 방법: Mock argv 확인과 TestRunner의 실제 OS 샌드박스 검증만 수행. 실제 Codex가 권한 위반을 시도하는 모델 검증은 하지 않음.
- 실행한 명령어: `실제 Codex 권한 위반 시나리오 미실행`
- 관련 파일 및 코드 위치: [src/dev_orchestra/infrastructure/codex.py:41](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/infrastructure/codex.py#L41), [src/dev_orchestra/infrastructure/test_runner.py:14](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/infrastructure/test_runner.py#L14)
- 실패 원인: 실행하지 않았거나 현재 단계의 적용 범위 밖입니다. PASS로 해석하지 않습니다.
- 필요한 수정 사항: 차단 결함 수정 후 별도 검증 계획과 실행 환경·인증·사용량 조건을 확인해 수행합니다.
- 연결된 결함: 없음

실제 결과:

```text
read-only 옵션의 존재만으로 호스트 전체 읽기 제한·MCP 제한·프로젝트 설정 우회 방지까지 통과 처리하지 않음.
```

<a id="n03"></a>
### N03 — 실제 서버의 인증 만료·사용량 한도 응답

- 중요도: **권장**
- 상태: **NOT_TESTED**
- 검증 방법: 인증 실패 종료 코드는 B01의 Mock으로 확인. 실제 로그인 정보를 바꾸거나 서버 오류를 유도하지 않음.
- 실행한 명령어: `실제 인증 변경·모델 요청 미실행`
- 관련 파일 및 코드 위치: [src/dev_orchestra/infrastructure/codex.py:69](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/infrastructure/codex.py#L69)
- 실패 원인: 실행하지 않았거나 현재 단계의 적용 범위 밖입니다. PASS로 해석하지 않습니다.
- 필요한 수정 사항: 차단 결함 수정 후 별도 검증 계획과 실행 환경·인증·사용량 조건을 확인해 수행합니다.
- 연결된 결함: 없음

실제 결과:

```text
실제 서버 401/429, 토큰 만료/갱신 동작은 미검증.
```

<a id="n04"></a>
### N04 — 모든 Git·파일·DB 경계에서의 강제 종료 복구

- 중요도: **필수**
- 상태: **NOT_TESTED**
- 검증 방법: E01은 실제 SIGKILL 단일 지점이며 기존 단위 테스트와 G06도 확인. 커밋 직후 DB 기록 전, 부분 패치, artifact rename 직후 등 모든 지점은 실행하지 않음.
- 실행한 명령어: `전 경계 장애 주입 미실행`
- 관련 파일 및 코드 위치: [src/dev_orchestra/orchestration/runner.py:173](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/orchestration/runner.py#L173), [src/dev_orchestra/infrastructure/files.py:41](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/infrastructure/files.py#L41)
- 실패 원인: 실행하지 않았거나 현재 단계의 적용 범위 밖입니다. PASS로 해석하지 않습니다.
- 필요한 수정 사항: 차단 결함 수정 후 별도 검증 계획과 실행 환경·인증·사용량 조건을 확인해 수행합니다.
- 연결된 결함: 없음

실제 결과:

```text
모든 강제 종료 지점의 자동 복구 보장은 미검증. 현재 코드는 dirty/HEAD 불일치 시 보존하고 개입을 요구하는 경로가 있음.
```

<a id="n05"></a>
### N05 — Linux bwrap·Java·JavaScript 도구 체인

- 중요도: **권장**
- 상태: **NOT_TESTED**
- 검증 방법: 검증 환경은 macOS/Python으로 한정.
- 실행한 명령어: `해당 플랫폼·도구 실행하지 않음`
- 관련 파일 및 코드 위치: [src/dev_orchestra/infrastructure/test_runner.py:34](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/infrastructure/test_runner.py#L34)
- 실패 원인: 실행하지 않았거나 현재 단계의 적용 범위 밖입니다. PASS로 해석하지 않습니다.
- 필요한 수정 사항: 차단 결함 수정 후 별도 검증 계획과 실행 환경·인증·사용량 조건을 확인해 수행합니다.
- 연결된 결함: 없음

실제 결과:

```text
Linux와 Maven/Gradle/Jest/Vitest의 출력·캐시·JUnit 방언 호환성 미검증.
```

<a id="na01"></a>
### NA01 — FastAPI·React·SSE 엔드포인트 자체

- 중요도: **권장**
- 상태: **NOT_APPLICABLE**
- 검증 방법: 요청에 따라 GUI/API 신규 구현은 검증 범위 밖으로 분류. 준비 구조는 V05/F05/E03에서 별도 평가.
- 실행한 명령어: `GUI 서버·브라우저 테스트 미실행`
- 관련 파일 및 코드 위치: [docs/GUI-REQUIREMENTS.md:1](../docs/GUI-REQUIREMENTS.md#L1)
- 실패 원인: 실행하지 않았거나 현재 단계의 적용 범위 밖입니다. PASS로 해석하지 않습니다.
- 필요한 수정 사항: GUI 미구현 항목은 추후 별도 구현·검증합니다.
- 연결된 결함: 없음

실제 결과:

```text
구현하지 않은 GUI가 정상 동작한다고 판정하지 않음.
```

<a id="v01"></a>
### V01 — 기존 전체 테스트 재실행

- 중요도: **필수**
- 상태: **PASS**
- 검증 방법: 기존 테스트 파일을 수정하지 않고 일반 실행 및 실제 샌드박스 선택 실행을 수행.
- 실행한 명령어: `CMD-BASE`
- 관련 파일 및 코드 위치: [tests/test_workflow.py:96](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/tests/test_workflow.py#L96), [tests/test_security.py:70](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/tests/test_security.py#L70)
- 실패 원인: 없음. 이 항목에서 실제로 검증한 범위에 한한 통과입니다.
- 필요한 수정 사항: 필요 없음. 단, 별도 실패 항목 및 미검증 범위는 그대로 남습니다.
- 연결된 결함: 없음

실제 결과:

```text
일반 실행 37 PASS/1 SKIP, native 실행 38 PASS. 이는 아래 48개 품질 항목 모두가 통과했다는 뜻이 아님.
```

<a id="v02"></a>
### V02 — 린트 및 포맷 검사

- 중요도: **권장**
- 상태: **PASS**
- 검증 방법: 수정 없이 ruff check/format --check 수행.
- 실행한 명령어: `CMD-LINT`
- 관련 파일 및 코드 위치: [pyproject.toml:25](../pyproject.toml#L25)
- 실패 원인: 없음. 이 항목에서 실제로 검증한 범위에 한한 통과입니다.
- 필요한 수정 사항: 필요 없음. 단, 별도 실패 항목 및 미검증 범위는 그대로 남습니다.
- 연결된 결함: 없음

실제 결과:

```text
All checks passed; 24 files already formatted.
```

<a id="v03"></a>
### V03 — 설치된 Codex CLI 옵션 호환성 확인

- 중요도: **필수**
- 상태: **PASS**
- 검증 방법: 현재 설치된 CLI의 version, global/exec help 및 CodexClient.check 실행. 모델 실행은 하지 않음.
- 실행한 명령어: `CMD-CODEX`
- 관련 파일 및 코드 위치: [src/dev_orchestra/infrastructure/codex.py:26](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/infrastructure/codex.py#L26)
- 실패 원인: 없음. 이 항목에서 실제로 검증한 범위에 한한 통과입니다.
- 필요한 수정 사항: 필요 없음. 단, 별도 실패 항목 및 미검증 범위는 그대로 남습니다.
- 연결된 결함: 없음

실제 결과:

```text
codex-cli 0.166.0. 요구하는 exec 옵션이 도움말에 존재하고 check 통과. 실제 0.166.0 모델 실행은 N01.
```

<a id="v04"></a>
### V04 — 저장된 Codex 로그인 상태 조회

- 중요도: **권장**
- 상태: **PASS**
- 검증 방법: codex login status만 실행. 저장된 계정 상태 조회이며 실제 서버 요청의 인증 성공·사용량 한도 확인을 뜻하지 않음.
- 실행한 명령어: `CMD-CODEX`
- 관련 파일 및 코드 위치: [src/dev_orchestra/infrastructure/codex.py:34](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/infrastructure/codex.py#L34)
- 실패 원인: 없음. 이 항목에서 실제로 검증한 범위에 한한 통과입니다.
- 필요한 수정 사항: 필요 없음. 단, 별도 실패 항목 및 미검증 범위는 그대로 남습니다.
- 연결된 결함: 없음

실제 결과:

```text
Logged in using ChatGPT. 이번 검증의 실제 모델 호출은 0회이며 비용을 발생시키는 요청은 하지 않음.
```

<a id="v05"></a>
### V05 — 공통 서비스 재사용 및 상태 JSON 직렬화

- 중요도: **필수**
- 상태: **PASS**
- 검증 방법: CLI와 독립적으로 RunService를 호출한 A02/A03/H01 및 상태 JSON 직렬화 실행. 서비스 모듈의 Typer 의존성도 검사.
- 실행한 명령어: `CMD-SOURCE`
- 관련 파일 및 코드 위치: [src/dev_orchestra/application/service.py:17](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/application/service.py#L17), [src/dev_orchestra/cli.py:16](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/cli.py#L16)
- 실패 원인: 없음. 이 항목에서 실제로 검증한 범위에 한한 통과입니다.
- 필요한 수정 사항: 필요 없음. 단, 별도 실패 항목 및 미검증 범위는 그대로 남습니다.
- 연결된 결함: 없음

실제 결과:

```text
서비스 직접 호출·json.dumps 성공, application/service.py의 Typer import 없음. HTTP 비동기성·OpenAPI DTO·여러 API 프로세스 안전성은 이 통과 범위에 포함하지 않음.
```

<a id="v06"></a>
### V06 — 회차별 로그 파일 조회 및 역할 필터

- 중요도: **필수**
- 상태: **PASS**
- 검증 방법: 실제 A02 TestRunner 로그를 RunService.log_paths(tester 필터)와 CLI logs로 조회.
- 실행한 명령어: `CMD-SOURCE`
- 관련 파일 및 코드 위치: [src/dev_orchestra/application/service.py:181](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/application/service.py#L181), [src/dev_orchestra/cli.py:131](https://github.com/Dobby90/dev-orchestra/blob/5f93bde38eda2279c7c51d486cc5a47d65598bfa/src/dev_orchestra/cli.py#L131)
- 실패 원인: 없음. 이 항목에서 실제로 검증한 범위에 한한 통과입니다.
- 필요한 수정 사항: 필요 없음. 단, 별도 실패 항목 및 미검증 범위는 그대로 남습니다.
- 연결된 결함: 없음

실제 결과:

```text
A02 tester 로그 1개, 98바이트 조회. 기본 파일 조회 통과. SSE·재접속 커서·제어 이벤트는 F05 및 NA01로 분리.
```

## 재현 스크립트 원문

아래 스크립트는 검증 전용이며 제품 코드가 아닙니다. 각 스크립트는 자신의 디렉터리를 ROOT로 사용합니다. 새 임시 디렉터리에 세 파일을 저장하고 프로젝트 루트에서 PYTHONPATH에 `src`와 해당 임시 디렉터리를 지정해 순서대로 실행하면 됩니다. 실제 모델은 호출하지 않습니다. macOS 샌드박스 실행 권한과 프로젝트 Python 의존성이 필요합니다. 실패 관찰은 FAIL JSON으로 출력하므로 스크립트 프로세스 종료 코드 0을 모든 항목의 통과로 해석하지 않아야 합니다.

<details>
<summary>audit.py — SHA-256 ebeab5e57fcf72072e87d3fb21daaf3981c01f52ae58b2104d60b007e87baddc</summary>

```python
import contextlib
import difflib
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import yaml
from typer.testing import CliRunner
from dev_orchestra.agents.policy import apply_patch
from dev_orchestra.application.service import RunService
from dev_orchestra.cli import app
from dev_orchestra.config import Config, Codex
from dev_orchestra.domain.models import AgentRequest, AgentResult, Coverage, Criterion, Issue, PipelineError
from dev_orchestra.infrastructure.codex import CodexClient
from dev_orchestra.infrastructure.git import Git
from dev_orchestra.infrastructure.locking import lock
from dev_orchestra.infrastructure.process import execute
from dev_orchestra.infrastructure.store import Store
from dev_orchestra.infrastructure.test_runner import TestRunner, parse_junit
from dev_orchestra.orchestration.runner import Runner

ROOT = Path(__file__).resolve().parent
RESULTS = []

def poll(fn, timeout=5):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        if fn(): return
        time.sleep(.03)
    raise RuntimeError('probe synchronization timeout')

def diff(name, before, after):
    return ''.join(difflib.unified_diff(before.splitlines(True),after.splitlines(True), fromfile='a/'+name if before else '/dev/null',tofile='b/'+name))

def setup(name, config_changes=None):
    root=ROOT/name; root.mkdir(exist_ok=True)
    repo=root/'repo';repo.mkdir()
    subprocess.run(['git','init','-q','-b','main',str(repo)],check=True)
    git=Git(repo);git.call('config','user.name','Readiness audit');git.call('config','user.email','audit@example.invalid')
    (repo/'main.py').write_text('value = 0\n');git.commit('검증용 초기 커밋')
    spec=root/'spec.md';spec.write_text('value를 1로 변경하고 정상 동작을 테스트한다.')
    data={'storage':{'database':str(root/'state.db'),'runs_directory':str(root/'runs'),'worktrees_directory':str(root/'worktrees')},'tests':{'commands':{'unit':{'argv':[sys.executable,'-m','pytest','tests','--junitxml=build/junit.xml','-q'],'junit_report':'build/junit.xml'}}}}
    if config_changes:
        for k,v in config_changes.items():data[k]=v
    cfg=Config.model_validate(data);svc=RunService(cfg);rid=svc.create(repo,spec)
    return root,repo,git,cfg,svc,rid

class Agent:
    def __init__(self, develop=False, write_tests=False, block=0):
        self.calls=[];self.develop=develop;self.write_tests=write_tests;self.block=block
    def run(self,req,directory,stop):
        self.calls.append(req.role)
        if req.role=='planner':return AgentResult(summary='계획',plan='value 변경',criteria=[Criterion(id='AC1',description='value == 1')])
        if req.role=='developer' and self.develop or req.role=='fixer':
            p=Path(req.worktree)/'main.py';old=p.read_text()
            return AgentResult(summary='구현',patch=diff('main.py',old,'value = 1\n') if old!='value = 1\n' else '')
        if req.role=='tester':
            p=Path(req.worktree)/'tests/test_value.py'
            content="from pathlib import Path\nimport runpy\ndef test_value():\n    assert runpy.run_path(str(Path(__file__).resolve().parents[1] / 'main.py'))['value'] == 1\n"
            return AgentResult(summary='테스트',patch=diff('tests/test_value.py','',content) if self.write_tests and not p.exists() else '',command_ids=['unit'],coverage=[Coverage(criterion_id='AC1',command_ids=['unit'])])
        if req.role=='reviewer' and req.iteration<self.block:
            return AgentResult(summary='문제',issues=[Issue(id='I1',severity='high',file='main.py',line=1,description='값 오류',evidence='value가 1이어야 함',suggestion='값 변경')])
        return AgentResult(summary='확인')

class Tests:
    def run(self,*args):return {'passed':True,'commands':[{'exit_code':0,'total':1}]}

def record(id, fn):
    try:
        ok,actual=fn()
        row={'id':id,'status':'PASS' if ok else 'FAIL','actual':actual}
    except Exception as e:
        row={'id':id,'status':'NOT_TESTED','actual':f'검증 도구 오류: {type(e).__name__}: {e}'}
    RESULTS.append(row);print(json.dumps(row,ensure_ascii=False),flush=True)


def basic_cli():
    root,repo,git,cfg,s,r=setup('cli')
    config=root/'config.yaml';config.write_text(yaml.safe_dump(cfg.model_dump(mode='json')))
    def sync(self,rid,detached=False):Runner(self.store,Agent(),Tests()).execute(rid);return 0
    cli=CliRunner();commands=[['status',r],['list'],['logs',r],['report',r],['approve',r]]
    with patch.object(RunService,'start',sync):
        result=cli.invoke(app,['--config',str(config),'run','--repo',str(repo),'--spec',str(root/'spec.md')])
    created=s.store.list()[0]['id']; outcomes={'run':result.exit_code}
    for cmd in commands:
        cmd=[created if x==r else x for x in cmd]
        v=cli.invoke(app,['--config',str(config),*cmd]);outcomes[cmd[0]]=v.exit_code
    v=cli.invoke(app,['--config',str(config),'status','missing']);outcomes['unknown-status']=v.exit_code
    return all(outcomes[k]==0 for k in ['run','status','list','logs','report','approve']) and outcomes['unknown-status']==1, outcomes

def real_mock_flow(fix=False):
    root,repo,git,cfg,s,r=setup('flow-fix' if fix else 'flow-success')
    original=git.sha();(repo/'main.py').write_text('value = 0\n# 사용자 미커밋 변경\n')
    a=Agent(develop=not fix,write_tests=True);Runner(s.store,a,TestRunner()).execute(r);d=s.store.get(r)
    out={'state':d['state'],'calls':a.calls,'tests':d.get('test_result',{}).get('execution'),'iteration':d['iteration'],'original_unchanged':git.sha()==original and '# 사용자' in (repo/'main.py').read_text()}
    if d['state']=='waiting_approval':s.approve(r);out['approved_state']=s.store.get(r)['state']
    return out.get('approved_state')=='completed' and out['original_unchanged'] and (not fix or a.calls.count('tester')==2),out

def zero_budget():
    *_,s,r=setup('zero-budget',{'workflow':{'max_fix_attempts':0}})
    a=Agent(block=1);Runner(s.store,a,Tests()).execute(r);d=s.store.get(r)
    return d['state']=='needs_input' and 'fixer' not in a.calls,{'state':d['state'],'calls':a.calls}

def severity_policy():
    out={}
    for sev in ['critical','high','medium','low']:
        *_,s,r=setup('severity-'+sev,{'workflow':{'max_fix_attempts':0}})
        class Review(Agent):
            def run(self,req,*args):
                if req.role=='reviewer':return AgentResult(summary='리뷰',issues=[Issue(id='I',severity=sev,file='main.py',line=1,description='증거',evidence='value',suggestion='변경')])
                return super().run(req,*args)
        Runner(s.store,Review(),Tests()).execute(r);out[sev]=s.store.get(r)['state']
    return out=={'critical':'needs_input','high':'needs_input','medium':'waiting_approval','low':'waiting_approval'},out

def fake_codex(name,mode):
    path=ROOT/(name+'-codex');path.write_text(f'''#!{sys.executable}
import sys,time,json
from pathlib import Path
if '--help' in sys.argv:
    {'time.sleep(60)' if mode=='hang-help' else "print('--output-schema --ephemeral --ignore-user-config --json')"}
    sys.exit(0)
if {mode!r} == 'auth':
    print('authentication failed',file=sys.stderr);sys.exit(1)
if {mode!r} == 'timeout':time.sleep(60)
result = {'bad-json' if mode=='bad-json' else json.dumps(AgentResult(summary='확인').model_dump())!r}
Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text(result)
print('{{"type":"turn.completed"}}')
''');path.chmod(.0.__class__(0) if False else 0o755);return path

def protocol_failures():
    out={}
    for mode in ['bad-json','auth','timeout']:
        executable=fake_codex(mode,mode);client=CodexClient(Codex(command=str(executable),timeout_seconds=1))
        req=AgentRequest(run_id='audit',role='reviewer',worktree=str(ROOT),iteration=0,input_sha='sha',context={})
        try:client.run(req,ROOT/('protocol-'+mode));out[mode]='unexpected success'
        except PipelineError as e:out[mode]=str(e)
    return all('unexpected' not in v for v in out.values()),out

def help_timeout():
    executable=fake_codex('hang','hang-help')
    p=subprocess.Popen([sys.executable,'-c',f'from dev_orchestra.infrastructure.codex import CodexClient;from dev_orchestra.config import Codex;CodexClient(Codex(command={str(executable)!r},timeout_seconds=1)).check()'],start_new_session=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    try:
        time.sleep(1.5);alive=p.poll() is None
        return not alive,{'still_running_after_seconds':1.5,'timeout_configured_seconds':1,'alive':alive}
    finally:
        with contextlib.suppress(ProcessLookupError):os.killpg(p.pid,signal.SIGKILL)
        p.wait()

def log_secrets():
    value='READINESS_CANARY_123456'
    rc,_=execute([sys.executable,'-c',f'import json; print(json.dumps({{"token":{value!r},"Authorization":"Bearer {value}"}}))'],ROOT,5,ROOT/'secrets.log')
    leaked=value in (ROOT/'secrets.log').read_text()
    return not leaked,{'exit_code':rc,'synthetic_secret_persisted':leaked,'format':'JSON token / Authorization Bearer'}

def child_survives():
    pidfile=ROOT/'descendant.pid'
    code="import signal,time,os;from pathlib import Path;signal.signal(signal.SIGTERM,signal.SIG_IGN);Path("+repr(str(pidfile))+").write_text(str(os.getpid()));time.sleep(60)"
    parent="import subprocess,sys,time;subprocess.Popen([sys.executable,'-c',"+repr(code)+"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);time.sleep(60)"
    error=''
    try:execute([sys.executable,'-c',parent],ROOT,1,ROOT/'descendant.log')
    except PipelineError as e:error=str(e)
    pid=int(pidfile.read_text());alive=True
    try:os.kill(pid,0)
    except ProcessLookupError:alive=False
    finally:
        with contextlib.suppress(ProcessLookupError):os.kill(pid,signal.SIGKILL)
    marker=json.loads((ROOT/'descendant.process.json').read_text())
    return not alive,{'error':error,'descendant_alive_after_timeout':alive,'marker_finished':marker['finished'],'cleanup':'검증용 자식 PID를 종료함'}

def stale_sha_approval():
    root,repo,git,cfg,s,r=setup('stale-proof')
    class Race(Runner):
        def report(self,rid):
            state=self.store.get(rid)
            if state['state']=='reporting' and not (root/'raced').exists():
                # Model a user/external tool commit in the report/finalization window.
                target=Path(state['worktree']);(target/'main.py').write_text('value = 999\n')
                Git(target).commit('검증 이후 외부 변경 재현');(root/'raced').touch()
            return super().report(rid)
    Race(s.store,Agent(),Tests()).execute(r);d=s.store.get(r)
    s.approve(r)
    result={'state_after_approve':s.store.get(r)['state'],'test_sha':d['test_result']['sha'],'review_sha':d['review_result']['sha'],'final_sha':d['final_sha'],'current_sha':d['current_sha']}
    return d['test_result']['sha']==d['final_sha'],result

def weakening_tests():
    root,repo,git,cfg,s,r=setup('weaken-tests')
    (repo/'tests').mkdir();f=repo/'tests/test_guard.py';f.write_text('def test_guard():\n    assert False\n');git.commit('기존 실패 테스트 추가')
    (repo/'build').mkdir()
    before=TestRunner().run(repo,cfg.tests,root/'before')
    apply_patch(repo,diff('tests/test_guard.py',f.read_text(),'def test_guard():\n    assert True\n'),'tester',['tests'])
    after=TestRunner().run(repo,cfg.tests,root/'after')
    return not after['passed'],{'before_passed':before['passed'],'after_passed':after['passed'],'test_agent_patch_accepted':True,'production_unchanged':(repo/'main.py').read_text()=='value = 0\n'}

def git_filter():
    root,repo,git,cfg,s,r=setup('git-filter')
    target=root/'outside-worktree-canary';script=root/'filter.py'
    script.write_text(f'import sys\nfrom pathlib import Path\nPath({str(target)!r}).write_text("executed")\nsys.stdout.write(sys.stdin.read())\n')
    git.call('config','filter.readiness.clean',f'{sys.executable} {script}')
    (repo/'.gitattributes').write_text('main.py filter=readiness\n');git.commit('필터 설정용 속성 추가')
    target.unlink(missing_ok=True)
    apply_patch(repo,diff('main.py','value = 0\n','value = 1\n'),'developer',['tests']);git.commit('호스트 커밋 재현')
    return not target.exists(),{'unsandboxed_clean_filter_executed':target.exists(),'write_location':'임시 저장소 외부의 검증용 파일','precondition':'저장소에 clean filter driver 설정이 존재함'}

def state_and_migrations():
    p=ROOT/'migration.db';db=sqlite3.connect(p);db.execute('create table schema_version(version integer primary key)');db.execute('insert into schema_version values(999)');db.commit();db.close()
    store=Store(p);store.create({'id':'r'});store.update('r','invented_state')
    versions=[x[0] for x in sqlite3.connect(p).execute('select version from schema_version')]
    return False,{'accepted_state':store.get('r')['state'],'schema_versions':versions,'future_schema_rejected':False}

def report_after_approve():
    *_,s,r=setup('report-stale');Runner(s.store,Agent(),Tests()).execute(r);s.approve(r)
    text=s.report(r)
    return '**state**: completed' in text,{'database_state':s.store.get(r)['state'],'report_still_waiting_approval':'**state**: waiting_approval' in text}

def yaml_error():
    path=ROOT/'bad.yaml';path.write_text('project: [\n')
    result=CliRunner().invoke(app,['--config',str(path),'list'])
    return result.exit_code==1 and bool(result.output.strip()),{'exit_code':result.exit_code,'output':result.output,'exception_type':type(result.exception).__name__}

def early_failure_report():
    root,repo,git,cfg,s,r=setup('worker-early',{'codex':{'command':str(ROOT/'does-not-exist')}})
    s.start(r);d=s.store.get(r)
    return d['state']=='failed' and (Path(d['directory'])/'final-report.md').exists(),{'state':d['state'],'error':d.get('error'),'report_exists':(Path(d['directory'])/'final-report.md').exists()}

def crash_resume():
    root,repo,git,cfg,s,r=setup('crash-resume')
    ready=root/'ready'
    code=f'''from audit import *
s=RunService(Config.model_validate_json(Path({str(root/'config.json')!r}).read_text()))
class Sleeping(Agent):
 def run(self,req,*args):
  if req.role=='developer':
   Path({str(ready)!r}).touch();time.sleep(60)
  return super().run(req,*args)
Runner(s.store,Sleeping(),Tests()).execute({r!r})
'''
    (root/'config.json').write_text(cfg.model_dump_json())
    p=subprocess.Popen([sys.executable,'-c',code],start_new_session=True,cwd=ROOT,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
    try:
        poll(ready.exists);os.kill(p.pid,signal.SIGKILL);p.wait()
        previous=s.store.get(r)['state'];s.resume(r);a=Agent();Runner(s.store,a,Tests()).execute(r)
        return s.store.get(r)['state']=='waiting_approval' and 'planner' not in a.calls,{'state_after_sigkill':previous,'resumed_calls':a.calls,'final_state':s.store.get(r)['state']}
    finally:
        if p.poll() is None:p.kill();p.wait()
        p.stderr.close()

def queued_cancel():
    root,repo,git,cfg,s,r=setup('queue-cancel');ready=root/'start'
    (root/'config.json').write_text(cfg.model_dump_json())
    code=f'from audit import *; s=RunService(Config.model_validate_json(Path({str(root/"config.json")!r}).read_text()));Path({str(ready)!r}).touch();Runner(s.store,Agent(),Tests()).execute({r!r})'
    with lock(s.store.path.with_suffix('.worker.lock')):
        p=subprocess.Popen([sys.executable,'-c',code],cwd=ROOT,start_new_session=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        poll(ready.exists);time.sleep(.2);s.cancel(r);time.sleep(.4)
        during=s.store.get(r);alive=p.poll() is None
    p.wait(timeout=5)
    return not alive and during['state']=='cancelled',{'while_other_run_holds_lock':{'state':during['state'],'control':during['control'],'worker_alive':alive},'after_releasing_other_run':s.store.get(r)['state']}

def branch_collision():
    root,repo,git,cfg,s,r=setup('branch-collision');d=s.store.get(r);git.call('branch',d['branch'],d['base_sha']);sha=git.sha()
    Runner(s.store,Agent(),Tests()).execute(r);d=s.store.get(r)
    return d['state']=='failed' and git.sha()==sha and not Path(d['worktree']).exists(),{'state':d['state'],'original_sha_preserved':git.sha()==sha,'error':d.get('error')}

def approval_after_cleanup():
    root,repo,git,cfg,s,r=setup('cleanup-approval');Runner(s.store,Agent(),Tests()).execute(r);s.approve(r);d=s.store.get(r);s.cleanup(r)
    (repo/'main.py').write_text('value = 888\n');new=git.commit('외부 변경용 커밋');git.call('update-ref','refs/heads/'+d['branch'],new)
    state=s.status(r)['state']
    return state!='completed',{'status_after_feature_ref_changed':state,'feature_sha':new,'approved_sha':d['final_sha'],'worktree_exists':Path(d['worktree']).exists()}

def junit_inconsistent():
    p=ROOT/'inconsistent.xml';p.write_text('<testsuite tests="1" errors="1"><testcase name="ok"/></testsuite>');result=parse_junit(p)
    return result['failed']>0,{'declared_suite_errors':1,'parsed':result}

def controls_events():
    *_,s,r=setup('event-control');before=s.store.events(r);s.store.control(r,'cancel');after=s.store.events(r)
    return len(after)>len(before),{'events_before':len(before),'events_after_cancel_request':len(after),'control':s.store.get(r)['control']}

if __name__=='__main__':
    cases=[('A01',basic_cli),('A02',lambda:real_mock_flow(False)),('A03',lambda:real_mock_flow(True)),('A04',zero_budget),('A05',severity_policy),('B01',protocol_failures),('B02',help_timeout),('B03',log_secrets),('B04',child_survives),('C01',stale_sha_approval),('C02',git_filter),('C03',branch_collision),('D01',weakening_tests),('D02',junit_inconsistent),('E01',crash_resume),('E02',queued_cancel),('E03',state_and_migrations),('F01',report_after_approve),('F02',yaml_error),('F03',early_failure_report),('F04',approval_after_cleanup),('F05',controls_events)]
    for id,fn in cases:record(id,fn)
    (ROOT/'results.json').write_text(json.dumps(RESULTS,ensure_ascii=False,indent=2))

```

</details>

<details>
<summary>extra.py — SHA-256 08c6557fccfa756f7f857183e9f08897d5c1a8dd5acdb5c7b3309e92be957ac9</summary>

```python
from audit import *


def missing_review_fields():
    path=ROOT/'incomplete-codex'
    path.write_text(f'''#!{sys.executable}
from pathlib import Path
import sys
Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text('{{"summary":"review not supplied"}}')
''');path.chmod(0o755)
    req=AgentRequest(run_id='r',role='reviewer',worktree=str(ROOT),iteration=0,input_sha='sha',context={})
    result=CodexClient(Codex(command=str(path))).run(req,ROOT/'missing-review-output')
    return False,{'accepted_incomplete_json':True,'defaulted_issues':result.issues,'supplied_fields':list(result.model_fields_set)}


def baseline_freeze():
    root,repo,git,cfg,s,r=setup('baseline-freeze');old=s.store.get(r)['base_sha']
    (repo/'other.txt').write_text('基準分岐の移動');new=git.commit('기준 브랜치 이동')
    class Inspect(Agent):
        review=None
        def run(self,req,*args):
            if req.role=='reviewer':self.review=req.context
            return super().run(req,*args)
    a=Inspect(develop=True);Runner(s.store,a,Tests()).execute(r);d=s.store.get(r)
    return d['base_sha']==old and a.review['base_sha']==old and 'value = 1' in a.review['diff'] and 'other.txt' not in a.review['diff'],{'recorded_base_sha':d['base_sha'],'moved_branch_sha':new,'diff':a.review['diff'],'state':d['state']}


def no_push():
    root,repo,git,cfg,s,r=setup('no-push');remote=root/'remote.git'
    subprocess.run(['git','init','--bare','-q',str(remote)],check=True);git.call('remote','add','origin',str(remote))
    Runner(s.store,Agent(),Tests()).execute(r);s.approve(r)
    p=subprocess.run(['git','--git-dir',str(remote),'for-each-ref'],capture_output=True,text=True,check=True)
    return not p.stdout.strip(),{'local_bare_remote_refs_after_approve':p.stdout.strip(),'state':s.store.get(r)['state']}


def independent_sessions():
    root,repo,git,cfg,s,r=setup('sessions');path=root/'codex'
    path.write_text(f'''#!{sys.executable}
import sys,json,os
from pathlib import Path
from dev_orchestra.domain.models import AgentResult, Criterion, Coverage
text=sys.stdin.read();data=json.loads(text.split('UNTRUSTED TASK DATA:\\n',1)[1]);role=data['role']
assert 'resume' not in sys.argv and '--ephemeral' in sys.argv and 'read-only' in sys.argv
result=AgentResult(summary=f'role={{role}} pid={{os.getpid()}}')
if role=='planner':result.plan='계획';result.criteria=[Criterion(id='AC1',description='검증')]
if role=='tester':result.command_ids=['unit'];result.coverage=[Coverage(criterion_id='AC1',command_ids=['unit'])]
Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text(result.model_dump_json())
with open({str(root/'pids.log')!r},'a') as f:f.write(json.dumps({{'pid':os.getpid(),'role':role,'argv':sys.argv,'context_keys':list(data['context'])}})+'\\n')
''');path.chmod(0o755)
    client=CodexClient(Codex(command=str(path)));Runner(s.store,client,Tests()).execute(r)
    rows=[json.loads(line) for line in (root/'pids.log').read_text().splitlines()]
    return len({x['pid'] for x in rows})==4,{'state':s.store.get(r)['state'],'roles':[x['role'] for x in rows],'distinct_pids':len({x['pid'] for x in rows}),'fresh_read_only_flags_checked':True}


def duplicate_execution():
    root,repo,git,cfg,s,r=setup('duplicate');(root/'config.json').write_text(cfg.model_dump_json());ready=root/'ready'
    code=f'''from audit import *
s=RunService(Config.model_validate_json(Path({str(root/'config.json')!r}).read_text()))
class Slow(Agent):
 def run(self,req,*args):
  with open({str(root/'calls.log')!r},'a') as f:f.write(req.role+'\\n')
  if req.role=='planner':Path({str(ready)!r}).touch();time.sleep(1)
  return super().run(req,*args)
Runner(s.store,Slow(),Tests()).execute({r!r})
'''
    p=subprocess.Popen([sys.executable,'-c',code],cwd=ROOT,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    try:
        poll(ready.exists)
        try:Runner(s.store,Agent(),Tests()).execute(r);rejected=False
        except PipelineError:rejected=True
        p.wait(timeout=5);calls=(root/'calls.log').read_text().splitlines()
        return rejected and len(calls)==4,{'duplicate_rejected':rejected,'calls':calls,'state':s.store.get(r)['state']}
    finally:
        if p.poll() is None:p.kill();p.wait()


def resume_artifact_validation():
    *_,s,r=setup('resume-tamper')
    class Fail(Agent):
        def run(self,req,*args):
            if req.role=='reviewer':raise PipelineError('검증용 리뷰 중단')
            return super().run(req,*args)
    Runner(s.store,Fail(),Tests()).execute(r)
    step=[x for x in s.store.steps(r) if x['role']=='tester'][0];Path(step['data']['artifact']).write_text('modified')
    s.resume(r);a=Agent();Runner(s.store,a,Tests()).execute(r)
    rejected=False
    try:s.approve(r)
    except PipelineError:rejected=True
    return 'reviewer' not in a.calls,{'resumed_calls':a.calls,'state_before_approval':s.store.get(r)['state'],'approval_blocked':rejected}


def integrity():
    dbs=list(ROOT.glob('**/state.db'));errors=[]
    for path in dbs:
        db=sqlite3.connect(path);check=db.execute('pragma integrity_check').fetchone()[0];fk=db.execute('pragma foreign_key_check').fetchall();db.close()
        if check!='ok' or fk:errors.append(str(path))
    return not errors,{'checked_databases':len(dbs),'integrity_errors':errors}


def actual_output_boundary():
    root,repo,git,cfg,s,r=setup('tracked-output');(repo/'build').mkdir();(repo/'build/source.py').write_text('protected');git.commit('추적 소스 출력 경로 재현')
    try:TestRunner().run(repo,cfg.tests,root/'test');blocked=False
    except PipelineError as e:blocked=True;message=str(e)
    return blocked,{'blocked':blocked,'error':message}


def sqlite_persistence():
    root,repo,git,cfg,s,r=setup('persistence');s.store.update(r,'planning',probe='saved');new=Store(cfg.storage.database)
    with new.connection() as db:
        journal=db.execute('pragma journal_mode').fetchone()[0];foreign=db.execute('pragma foreign_keys').fetchone()[0]
    events=new.events(r);cursor=events[-1]['id'];new.update(r,'developing');later=new.events(r,cursor)
    return new.get(r)['probe']=='saved' and journal=='wal' and foreign==1 and len(later)==1,{'reopened_value':new.get(r)['probe'],'journal':journal,'foreign_keys':foreign,'events_after_cursor':len(later)}


def actual_report_failure():
    p=ROOT/'suite-report.xml'
    out={}
    for label,content in [('empty','<testsuite/>'),('all-skipped','<testsuite><testcase><skipped/></testcase></testsuite>'),('bad-xml','<testsuite'),('suite-error','<testsuite><testcase><error>oops</error></testcase></testsuite>')]:
        p.write_text(content)
        try:out[label]=parse_junit(p)
        except PipelineError:out[label]='rejected'
    return out['empty']==out['all-skipped']==out['bad-xml']=='rejected' and out['suite-error']['failed']==1,out


def model_source_independence():
    from dev_orchestra.agents.prompts import prompt
    req=AgentRequest(run_id='r',role='reviewer',worktree=str(ROOT),iteration=0,input_sha='sha',context={'prd':'test','test_result':{'summary':'test'}})
    return 'Independent' in prompt(req) or 'Independently' in prompt(req),{'review_prompt_independent':True,'private_reasoning_prohibited':'private reasoning' in prompt(req),'developer_transcript_context':False}

if __name__=='__main__':
    for id,fn in [('G01',missing_review_fields),('G02',baseline_freeze),('G03',no_push),('G04',independent_sessions),('G05',duplicate_execution),('G06',resume_artifact_validation),('G07',integrity),('G08',actual_output_boundary),('G09',sqlite_persistence),('G10',actual_report_failure),('G11',model_source_independence)]:record(id,fn)
    (ROOT/'extra-results.json').write_text(json.dumps(RESULTS,ensure_ascii=False,indent=2))

```

</details>

<details>
<summary>final_probes.py — SHA-256 abc213434807514305da52e867a4921a95836bffb956ad9849f11cb810addfe3</summary>

```python
from audit import *


def native_stale():
    root,repo,git,cfg,s,r=setup('native-stale-proof2')
    class External(Runner):
        def report(self,rid):
            run=self.store.get(rid)
            if run['state']=='reporting' and not (root/'once').exists():
                wt=Path(run['worktree']);(wt/'main.py').write_text('value = 999\n');Git(wt,cfg.tests.output_paths).commit('검증 후 외부 커밋');(root/'once').touch()
            return super().report(rid)
    External(s.store,Agent(develop=True,write_tests=True),TestRunner()).execute(r);d=s.store.get(r)
    if d['state']=='waiting_approval':s.approve(r)
    after=TestRunner().run(Path(d['worktree']),cfg.tests,root/'retest')
    return d.get('final_sha')==d.get('test_result',{}).get('sha'),{'approved_state':s.store.get(r)['state'],'old_test_passed':d.get('test_result',{}).get('execution',{}).get('passed'),'test_sha':d.get('test_result',{}).get('sha'),'final_sha':d.get('final_sha'),'actual_retest_passed':after['passed']}


def worker_cli_cancel_resume():
    root,repo,git,cfg,s,unused=setup('real-worker-cli');stub=root/'codex';release=root/'release'
    stub.write_text(f'''#!{sys.executable}
import sys,json,time
from pathlib import Path
from dev_orchestra.domain.models import AgentResult,Criterion,Coverage
if '--help' in sys.argv:
 print('--output-schema --ephemeral --ignore-user-config --json');sys.exit(0)
data=json.loads(sys.stdin.read().split('UNTRUSTED TASK DATA:\\n',1)[1]);role=data['role']
if role=='developer' and not Path({str(release)!r}).exists():
 print('waiting',flush=True);time.sleep(60)
result=AgentResult(summary='검증')
if role=='planner':result.plan='계획';result.criteria=[Criterion(id='AC1',description='검증')]
if role=='tester':result.command_ids=['unit'];result.coverage=[Coverage(criterion_id='AC1',command_ids=['unit'])]
Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text(result.model_dump_json())
''');stub.chmod(0o755)
    # Only cancellation/pause is exercised here; it occurs before a test execution.
    data=cfg.model_dump(mode='json');data['codex']['command']=str(stub)
    config=root/'config.yaml';config.write_text(yaml.safe_dump(data));cli=CliRunner()
    opts=['--config',str(config)]
    result=cli.invoke(app,opts+['run','--repo',str(repo),'--spec',str(root/'spec.md'),'--detach'])
    rid=result.output.strip().splitlines()[0]
    poll(lambda:s.store.get(rid)['state']=='developing')
    d=s.store.get(rid);poll(lambda:bool(list(Path(d['directory']).glob('iterations/*/developer/*/*.process.json'))))
    cancelled=cli.invoke(app,opts+['cancel',rid]);poll(lambda:s.store.get(rid)['state']=='cancelled')
    marker=list(Path(d['directory']).glob('iterations/*/developer/*/*.process.json'))[0]
    return cancelled.exit_code==0 and json.loads(marker.read_text())['finished'],{'run_exit':result.exit_code,'cancel_exit':cancelled.exit_code,'state':s.store.get(rid)['state'],'process_marker':json.loads(marker.read_text())}


def forbidden_patch():
    root,repo,git,cfg,s,r=setup('unsafe-patch')
    blocked=[]
    for name,role in [('../escape','developer'),('.git/config','developer'),('main.py','tester')]:
        try:apply_patch(repo,diff(name,'value = 0\n','value = 1\n'),role,['tests'])
        except PipelineError:blocked.append(name)
    return len(blocked)==3,{'blocked':blocked,'original_source':(repo/'main.py').read_text()}


def max_three_and_history():
    root,repo,git,cfg,s,r=setup('max-three');a=Agent(block=99);Runner(s.store,a,Tests()).execute(r)
    steps=s.store.steps(r);test_steps=[x for x in steps if x['role']=='tester'];review_steps=[x for x in steps if x['role']=='reviewer']
    artifacts=[x['data']['artifact'] for x in steps if x['status']=='completed']
    return a.calls.count('fixer')==3 and len(test_steps)==len(review_steps)==4 and len(set(artifacts))==len(artifacts),{'state':s.store.get(r)['state'],'fix_count':a.calls.count('fixer'),'test_iterations':[x['iteration'] for x in test_steps],'review_iterations':[x['iteration'] for x in review_steps],'unique_artifacts':len(set(artifacts))}

if __name__=='__main__':
    for id,fn in [('C01',native_stale),('H01',worker_cli_cancel_resume),('H02',forbidden_patch),('H03',max_three_and_history)]:record(id,fn)
    (ROOT/'final-results.json').write_text(json.dumps(RESULTS,ensure_ascii=False,indent=2))

```

</details>

