# DevOrchestra

Codex CLI를 활용해 기획, 개발, 테스트 작성·실행, 독립적인 코드 리뷰, 제한된 횟수의 자동 수정을 수행하는 로컬 Python CLI입니다. 사용자 승인은 검증된 커밋을 승인 상태로 기록하며, 대상 프로젝트의 원격 Push나 Merge를 수행하지 않습니다.

이 저장소는 모노레포로 관리합니다. 공통 Python 백엔드는 `src/dev_orchestra`에 있으며, 향후 React 앱은 `apps/web`에 추가할 예정입니다. 현재 FastAPI와 React는 구현하지 않았습니다.

## 설치

Git, Python 3.11 이상, Codex CLI, OS 샌드박스가 필요합니다. Codex CLI 0.165.0을 기준으로 개발·검증했습니다. macOS에서는 Seatbelt를 사용하고, Linux에서는 `bwrap`이 필요합니다. 현재 MVP는 Windows를 지원하지 않습니다.

```sh
# uv가 설치된 경우
uv python install 3.12
uv sync --frozen
uv run devorchestra --help

# Python 3.11 이상이 이미 설치된 경우의 예시
python3.12 -m venv .venv
.venv/bin/python -m pip install -e .
```

현재 개발 작업 공간에는 독립 Python 3.12 런타임인 `.python`, uv 실행 파일이 있는 `.tools`, 가상환경인 `.venv`도 준비되어 있습니다. 이 디렉터리들은 Git에 포함되지 않습니다. 현재 작업 공간에서 잠금 파일에 맞춰 환경을 구성하고 테스트하려면 다음 명령을 사용합니다.

```sh
UV_CACHE_DIR="$PWD/.uv-cache" .tools/uv sync --frozen
.venv/bin/python -m pytest -q
```

Codex 인증은 `codex login`으로 별도 설정합니다. 인증과 사용 요금은 설치된 Codex의 설정 및 계정을 따릅니다. 일반 테스트에서는 모델을 호출하지 않습니다.

## 설정

`config.example.yaml`을 복사해 설정 파일을 만들고, 테스트용 인터프리터와 명령을 대상 프로젝트에 맞게 수정합니다. `--config` 옵션은 하위 명령 앞에 지정합니다.

YAML의 상대 경로는 설정 파일 위치를 기준으로 해석합니다. 기본 상태 저장 위치는 저장소 외부의 `~/.devorchestra`이며, 대상 저장소 내부에 상태나 Worktree를 저장하도록 설정하면 실행을 거부합니다.

테스트 명령 ID는 호스트 설정의 허용 목록입니다. 모델이 임의의 셸 명령을 만들어 실행할 수 없으며, 명령은 셸 없이 인자 배열로 실행합니다. 테스트 0건, 전체 건너뛰기, 보고서 누락·형식 오류를 통과 처리하지 않도록 JUnit XML 보고서를 필수로 요구합니다.

Java/Maven, Gradle, JavaScript 테스트 도구도 JUnit XML을 생성하도록 설정하면 사용할 수 있습니다. DevOrchestra가 대상 프로젝트의 의존성을 설치하거나, 호스트 설정 없이 테스트 명령을 선택하지는 않습니다.

- `write_paths`: Test Agent가 생성한 패치를 적용할 수 있는 테스트 경로입니다.
- `output_paths`: Git이 추적하지 않는 빌드·보고서 출력 디렉터리입니다. 추적 중인 소스 파일을 포함한 디렉터리는 허용하지 않습니다.

테스트에 필요한 의존성은 오프라인에서 사용할 수 있도록 미리 준비해야 합니다. Maven·Gradle·npm은 프로젝트별 캐시 및 출력 경로 설정이 추가로 필요할 수 있습니다. 테스트 중 외부 네트워크 접근은 차단합니다.

## 명령어

```sh
devorchestra --config config.yaml doctor
devorchestra --config config.yaml run --repo ~/projects/example --spec PRD.md --base main --max-fix-attempts 3
devorchestra --config config.yaml run --repo ~/projects/example --spec PRD.md --detach
devorchestra status RUN_ID
devorchestra list
devorchestra logs RUN_ID --follow --role reviewer
devorchestra pause RUN_ID
devorchestra resume RUN_ID
devorchestra resume RUN_ID --answers answers.md
devorchestra resume RUN_ID --max-fix-attempts 5
devorchestra cancel RUN_ID
devorchestra report RUN_ID
devorchestra approve RUN_ID
devorchestra cleanup RUN_ID
```

기본 저장 위치를 변경했다면 이후 명령에도 동일한 `--config` 또는 `--database`를 지정해야 합니다. `codex-pipeline`은 `devorchestra`와 동일하게 동작하는 별칭입니다. 조회 명령은 JSON으로 결과를 출력합니다.

| 상황 | 종료 코드 |
|---|---|
| `run`·`resume`의 실행 실패 또는 사용자 개입 필요 | `2` |
| 명령·입력 오류 | `1` |
| 최종 승인 대기 도달 | `0` |

일반 실행 중 `Ctrl+C`를 누르면 안전한 일시중지를 요청합니다. `--detach`로 실행한 Worker는 CLI 종료 후에도 계속 동작합니다. 취소는 하위 프로세스가 종료된 뒤 확정됩니다.

`cleanup`은 Worktree를 정리하되 브랜치, 커밋, 보고서, SQLite 이력을 보존합니다. 미커밋 변경이 있는 Worktree는 강제로 삭제하지 않습니다.

## 워크플로와 검증 근거

기본 실행 순서는 다음과 같습니다.

```text
queued → preparing → planning → developing → testing → reviewing
```

테스트가 실패하거나 통과를 차단하는 리뷰 문제가 발견되면 `fixing → testing → reviewing`을 반복합니다. 자동 수정은 기본 최대 3회입니다. 검증을 통과하면 보고서를 생성하고 `waiting_approval` 상태로 사용자 승인을 기다립니다.

- `needs_input`: PRD가 불명확하거나 수정 한도를 소진해 사용자 판단이 필요한 상태입니다.
- `failed`: 프로세스 실행 등에서 오류가 발생한 상태입니다.
- `resume`: 체크포인트를 검증하고 완료된 단계는 보존한 채 재개합니다. 수정 횟수는 초기화하지 않습니다.
- `approve`: 검증한 HEAD가 유지되고 미커밋 변경이 없으며 검증 자료가 변조되지 않았을 때 승인합니다.

모든 에이전트는 새로 생성한 읽기 전용 Codex 세션에서 실행하고 구조화된 결과를 반환합니다. 코드 변경은 호스트가 unified diff 패치를 검사·적용하는 방식으로 수행합니다. Developer와 Fix Agent는 기존 테스트를 수정할 수 없습니다.

파일 삭제·이름 변경, 바이너리 패치, 심볼릭 링크, 권한 변경은 사용자의 별도 처리가 필요합니다. Test Agent 패치는 설정된 테스트 경로에만 적용합니다. 자동 수정 반복 중에는 Planning 단계에서 확정한 인수 조건을 변경하지 않습니다. 모델이 생성한 테스트 대응 관계는 추적을 위한 자료이며, 요구사항 충족을 수학적으로 증명하지는 않습니다.

산출물은 `runs/RUN_ID/iterations/ITERATION/ROLE/attempt-NNN/` 아래에 회차·역할·시도별로 저장합니다. 각 시도에는 스키마, 응답, 로그, 결과, 프로세스 기록이 남습니다. 실행 디렉터리 최상위에는 최신 요약, PRD·설정 스냅샷, `final-report.md`를 저장합니다.

상태와 이벤트 이력은 SQLite에 저장하며 WAL, 외래 키, 짧은 트랜잭션을 사용합니다. OS 잠금으로 데이터베이스와 저장소별 실행을 직렬화합니다. 최종 결과와 승인은 Git 커밋 SHA에 연결됩니다. 승인한 결과가 변경되면 사용자 개입이 필요한 상태로 표시하고, 과거 승인 이력은 보존합니다.

## 안전성 및 현재 제약

- **기존 작업 환경 보존**: 원래 작업 디렉터리의 수정 내용과 현재 브랜치를 유지합니다. Worktree에는 기준 커밋의 코드만 가져오며, 미커밋 변경은 복사하지 않습니다.
- **Git 작업 제한**: Feature 브랜치와 Worktree는 Git 메타데이터를 공유합니다. 에이전트는 이를 수정할 수 없습니다. 호스트의 Git 명령은 hooks·서명·fsmonitor를 비활성화하고 대상 프로젝트를 Push하거나 Merge하지 않습니다.
- **테스트 격리**: 소스는 읽기 전용으로 두고 지정한 출력 디렉터리만 쓰기를 허용합니다. 임시 HOME과 선별한 환경변수를 사용하며, 네트워크는 차단합니다. macOS에서는 알려진 인증 정보 경로와 프로젝트의 `.env`·키 파일 읽기를 차단합니다. 다만 완전한 악성 코드 격리를 보장하지는 않으며, 다른 위치의 알려지지 않은 비밀 파일은 읽힐 수 있습니다. Linux의 bwrap 백엔드는 별도 배포 검증이 필요하고 읽기 허용 범위도 더 넓습니다.
- **Codex 읽기 범위**: 읽기 전용 샌드박스는 쓰기를 제한하지만 관련 없는 모든 호스트 파일의 읽기까지 차단하지는 않습니다. 신뢰할 수 없는 저장소는 전용 계정이나 컨테이너에서 실행해야 합니다.
- **프롬프트 인젝션과 테스트 무력화**: PRD나 저장소 파일은 호스트의 테스트 명령을 선택하거나 결과를 승인할 수 없습니다. 프롬프트 인젝션과 테스트의 의미적인 약화를 완전히 자동 탐지할 수는 없습니다.
- **수동 확인이 필요한 복구**: 커밋 직후 체크포인트 기록 전 종료, 외부에서 변경된 HEAD, 미커밋 변경, 살아 있는 고아 프로세스는 직접 확인해야 합니다. 데이터는 보존하며 자동 reset·clean으로 복구 성공을 가장하지 않습니다.
- **승인의 유효 범위**: 승인은 특정 SHA에 대한 이력입니다. 파일시스템 감시기는 없으며, `status`에서 현재 승인 유효성을 확인합니다.
- **로그와 민감정보**: 로그는 스트리밍하며 저장 전에 비밀값을 마스킹합니다. 프로세스별 출력은 최대 8 MiB입니다. 모델의 구조화 응답과 PRD 스냅샷에는 민감한 프로젝트 내용이 포함될 수 있으므로 상태 저장소를 비공개로 관리해야 합니다. 모든 비밀값 형식을 자동 식별할 수는 없습니다.
- **동시 실행**: SQLite 데이터베이스별로 Worker 하나만 실행합니다. 서로 다른 데이터베이스가 같은 Git common directory를 대상으로 작업해도 저장소 잠금으로 직렬화합니다.
- **구현 범위**: GUI, API 서버, 원격 실행, 대상 프로젝트 의존성 자동 설치, 자동 Push·Merge, 분산 큐는 포함하지 않습니다.

## 개발 및 검증

```sh
uv run pytest -q
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts

# 모델 사용량이 발생하므로 실행 전 사용자 승인이 필요합니다.
uv run python scripts/e2e.py

# 모델 호출 없이 실제 OS 샌드박스를 검증합니다.
DEVORCHESTRA_TEST_SANDBOX=1 uv run pytest tests/test_security.py -q
```

선택 실행하는 E2E는 임시 Git 저장소를 만들고, 실제 Codex 에이전트와 OS 샌드박스의 pytest를 실행합니다. 성공한 결과를 승인하고 원래 작업 디렉터리가 보존되는지 확인합니다. 실행 후 확인할 수 있도록 임시 산출물 위치를 출력하며, 실제 모델 실행의 실패를 성공으로 처리하지 않습니다.

상세 검증 결과는 [검증 기록](docs/VALIDATION.md)에서 확인할 수 있습니다.
