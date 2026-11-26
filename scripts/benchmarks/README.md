# 성능 비교 과제와 독립 채점

두 과제는 Python 표준 라이브러리만 사용합니다. `prepare`는 PRD·시작 코드·공개 단위 테스트가 들어 있는 독립 Git 저장소를 만듭니다. 같은 버전의 과제를 준비하면 경로와 준비 시각에 관계없이 시작 커밋 SHA가 같습니다. `grade`는 지정한 커밋의 사본을 OS 테스트 샌드박스에서 실행하고, 과제 저장소 밖에 둔 기대값과 결과를 비교합니다. 두 명령 모두 모델을 호출하지 않습니다.

| 과제 | 변경 요청 | 독립 검사 |
| --- | --- | --- |
| `label-list` | 단일 모듈의 라벨 파서에 선택적 중복 제거 추가 | 기본 호출의 호환성, 최초 표기와 순서, 유니코드 대소문자, 빈 입력 등 5개 |
| `usage-window` | 시간 구간별 사용량 집계의 경계 오류 수정 | 종료 시각 제외, 시작 시각 중복 이벤트, 서로 다른 UTC 오프셋과 날짜 경계, 빈 구간 등 6개 |

## 과제 준비와 채점

DevOrchestra 저장소 루트의 개발 환경에서 실행합니다. `prepare`에는 기존 상위 디렉터리 아래의 **새 경로**를 지정합니다.

```sh
BENCH_DIR="$(mktemp -d)"
.venv/bin/python scripts/benchmarks/compare.py prepare label-list "$BENCH_DIR/labels"
.venv/bin/python scripts/benchmarks/compare.py prepare usage-window "$BENCH_DIR/usage"
```

각 도구에는 생성한 과제 저장소와 그 안의 `PRD.md`만 전달합니다. 공개 검사는 각 과제 저장소에서 `python -m unittest discover -s tests`로 실행합니다. DevOrchestra에서 검증 명령을 자동 탐지하면 이 두 과제에는 `python -m pytest`가 선택되므로 다음 비교 실행에는 pytest가 설치된 개발 환경을 사용합니다. 독립 검사 입력과 기대값은 DevOrchestra의 `scripts/benchmarks/grading/`에 보관합니다. 실행 경로마다 `prepare`로 별도 저장소를 만들고 시작 SHA를 대조합니다.

```sh
.venv/bin/python scripts/benchmarks/compare.py grade label-list "$BENCH_DIR/labels" --revision HEAD
.venv/bin/python scripts/benchmarks/compare.py grade usage-window "$BENCH_DIR/usage" --revision HEAD
```

직접 수정한 결과는 먼저 해당 과제 저장소에 커밋합니다. DevOrchestra 결과는 저장소 경로와 `--revision`에 승인 대상 SHA를 지정합니다. 기본값 `HEAD`는 지정한 저장소의 현재 커밋입니다. 미커밋 변경은 점수에 포함되지 않습니다. 결과 JSON에는 실제 채점한 SHA, 통과 수, 전체 수, 사례별 판정이 들어갑니다. 종료 코드 0은 전체 통과, 1은 독립 검사 실패, 2는 환경·채점 실행 오류입니다. 기본 과제에서는 공개 검사가 모두 통과해도 독립 검사가 일부 실패해야 합니다.

기준 구현을 포함한 검증은 임시 저장소에서 다음과 같이 실행합니다. 현재 OS에서 DevOrchestra 테스트 샌드박스를 사용할 수 있어야 합니다.

```sh
DEVORCHESTRA_TEST_SANDBOX=1 .venv/bin/python -m pytest -q tests/test_comparison_tasks.py
```

## 작성된 테스트의 회귀 결함 검출력

`mutation_check.py`는 지정한 커밋의 `tests/`를 가져와 기준 구현과 과제별 결함 7개에 각각 실행합니다. 라벨 과제에는 기본값·중복 제거·유니코드·표기·순서·공백 처리를, 시간 구간 과제에는 양쪽 경계·오프셋·중복 시각·입력 순서·정수 반환·빈 구간을 넣었습니다. 각 구현은 새 임시 저장소와 OS 샌드박스를 사용합니다. 정상 구현에서 모든 테스트가 통과하고 같은 수의 테스트가 결함 구현에서 실행되어야 합니다. 채점기는 JUnit에 기록된 단언 실패만 검출로 계산합니다.

```sh
.venv/bin/python scripts/benchmarks/mutation_check.py usage-window /absolute/path/to/task --revision COMMIT_SHA
```

결과의 `killed`는 테스트가 잡은 결함, `survived`는 통과시킨 결함입니다. pytest가 여러 실패한 `subTest`를 한 테스트에 기록해 JUnit 실패 집계가 어긋나면 해당 결함을 표준 `unittest`로 다시 실행합니다. 정상 대조군과 실행한 테스트 식별자가 전부 일치하고 단언 실패가 재현된 경우만 인정하며, 해당 결함은 `unittest_rechecked`에 표시합니다. 같은 실행에서 Developer 완료 SHA와 Tester 완료 SHA를 각각 채점하면 Tester가 보탠 검출력을 확인할 수 있습니다. 위의 `DEVORCHESTRA_TEST_SANDBOX=1` 검사는 독립 기대값으로 기준 구현과 모든 결함을 교정합니다.

## 역할별 시간과 토큰

보존된 실험용 DB와 해당 Run의 산출물 디렉터리를 사용합니다.

```sh
.venv/bin/python scripts/measure_run.py --database /absolute/path/to/state.db \
  --run-id RUN_ID --version-label baseline --scenario normal --format table
```

표는 실행한 역할마다 한 행을 표시하며 실패·재시도·수정 회차를 포함합니다. `Stage s`는 해당 역할의 단계 시작부터 종료까지 경과를 더한 값으로, 중단 후 재개한 단계의 대기도 포함합니다. `CLI s`는 도구 응답 대기를 포함한 Codex 호출 시간의 합입니다. 두 시간은 겹칩니다. 역할별 시간을 합쳐도 큐 대기와 단계 사이 작업을 포함한 Run 전체 경과와 다를 수 있습니다.

`Input`에는 `Cached`가 포함됩니다. `Uncached`는 입력에서 캐시 입력을 뺀 값이고, `Total`은 입력과 출력의 합입니다. `Usage`가 `partial`이면 보고된 토큰만 합산했고 `unknown`이면 사용량을 확인할 수 없습니다. 시간 합계의 일부라도 누락되거나 토큰 값이 없으면 해당 칸에 `—`를 표시합니다. 보고된 0은 숫자 0으로 표시합니다.
