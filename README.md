# DevOrchestra

Codex CLI를 활용해 기획, 개발, 테스트 작성·실행, 독립적인 코드 리뷰, 제한된 횟수의 자동 수정을 수행하는 로컬 개발 도구입니다. Python CLI와 React GUI가 같은 서비스·SQLite 상태·검증 근거를 사용합니다. 최종 승인은 검증된 Git 커밋 SHA에 대한 이력으로 기록하며, 대상 프로젝트를 자동으로 Push하거나 Merge하지 않습니다.

주요 기능은 다음과 같습니다.

- 별도 Git Worktree에서 실행하고 체크포인트를 기준으로 일시중지·재개·취소합니다.
- 호스트가 허용한 테스트 명령과 JUnit XML, 독립 리뷰를 함께 검증합니다.
- GUI에서 프로젝트 등록, PRD 작성·업로드, 실행 전 확인, 로그·타임라인·테스트·리뷰·Diff·보고서 조회와 승인을 제공합니다.
- 설정 화면에서 에이전트별 모델·추론 강도를 미리 지정하고 실행 상세에서 설정과 토큰 사용량을 조회합니다.
- 독립 Dispatcher·Run Worker와 영속 이벤트·SSE로 브라우저 연결과 작업 실행을 분리합니다.
- 승인 대기 중 수정 요청·승인 보류, 검증 산출물 보존과 Worktree 정리를 지원합니다.

## 요구 환경

| 구분 | 요구 사항 |
|---|---|
| CLI | Python 3.11 이상, Git, 인증된 Codex CLI |
| 테스트 격리 | macOS Seatbelt 또는 Linux `bwrap` |
| 로컬 GUI/API | Python의 `web` extra |
| GUI 소스 개발·빌드 | Node 22.12 이상, npm |
| 브라우저 검사 | Playwright Chromium |

현재 검증된 사용 범위는 macOS의 단일 사용자·신뢰하는 로컬 프로젝트·Chromium입니다. Linux `bwrap`, Firefox/WebKit, 장시간 부하와 OS 재부팅·디스크 장애 복구는 별도 검증이 필요합니다. Windows, 원격 실행, 다중 사용자 운영, 분산 큐는 지원하지 않습니다. Git은 `add --pathspec-from-file`을 지원해야 합니다.

## 설치

저장소 루트에서 실행합니다. `uv`를 사용하면 잠금 파일에 맞춰 개발 환경을 준비할 수 있습니다.

```sh
uv python install 3.12
uv sync --frozen
.venv/bin/devorchestra --help

# GUI/API도 사용할 경우
uv sync --frozen --extra web
```

이미 Python이 설치되어 있다면 CLI를 다음과 같이 설치할 수도 있습니다. Python 명령 이름은 환경에 맞게 선택하세요.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
# GUI/API도 사용할 경우
.venv/bin/python -m pip install -e '.[web]'
```

Codex 인증은 `codex login`으로 별도 설정합니다. 실제 작업 실행에는 모델 사용량이 발생하며 일반 단위·통합 검사는 실제 모델을 호출하지 않습니다. 아래 명령 예시의 `devorchestra`는 가상환경을 활성화하거나 `.venv/bin/devorchestra`로 실행하세요. `codex-pipeline`도 같은 CLI의 별칭입니다.

## GUI 빠른 시작과 종료

Python 웹 환경을 준비한 뒤 저장소 루트에서 실행합니다. 예시의 허용 경로는 실제 프로젝트의 상위 디렉터리로 바꾸세요.

```sh
uv sync --frozen --extra web
./start.sh --allow-root ~/projects
```

`start.sh`는 `.venv`를 사용해 GUI를 빌드하고 SQLite를 준비한 뒤 GUI·API·Dispatcher를 백그라운드로 시작합니다. 별도 DB 서버는 필요하지 않습니다. 출력된 `http://127.0.0.1:8000` 주소로 접속하고 일회용 코드를 입력하세요. 브라우저는 자동으로 열지 않습니다. Dispatcher를 시작하면 기존 대기 작업도 실행할 수 있습니다.

| 옵션 | 동작 |
|---|---|
| `--allow-root 경로` | 프로젝트 등록을 허용할 상위 경로입니다. 여러 번 지정할 수 있으며 기본값은 이 저장소의 상위 디렉터리입니다. |
| `--port 8001` | 1024~65535 범위의 포트입니다. 기본값은 8000입니다. |
| `--config config.yaml` | CLI와 공유할 설정 파일입니다. 상대 경로는 명령을 실행한 디렉터리를 기준으로 해석합니다. |
| `--rebuild` | GUI 의존성도 `npm ci`로 재설치합니다. 기존 서비스를 종료한 뒤 사용합니다. |

새로 기동할 때는 항상 최신 React 소스를 빌드하므로 Node와 npm이 필요합니다. `node_modules`가 없으면 `npm ci`를 먼저 실행합니다. 이미 실행 중이면 기존 프로세스·포트를 재사용하고 새 로그인 코드만 발급합니다. GUI 소스·포트·허용 경로 변경을 반영하려면 종료 후 다시 시작하세요.

```sh
./stop.sh

# 미완료 작업도 취소할 경우
./stop.sh --cancel-active

# 별도 설정 파일로 기동한 경우 종료에도 같은 파일을 지정합니다.
./stop.sh --config config.yaml
```

일반 종료는 해당 서비스가 시작한 API·Dispatcher를 종료하며 실행 중 Run Worker는 유지합니다. 재사용한 외부 Dispatcher도 유지합니다. 중복 시작과 포트 충돌은 기존 프로세스를 임의로 종료하지 않습니다. 구버전 DB 업그레이드가 필요한 경우에는 아래 마이그레이션 조건을 따르며, 현재 스키마인 DB는 불필요하게 업그레이드하지 않습니다.

로그인 코드는 **발급 후 5분간 한 번** 사용할 수 있습니다. 새 세션과 브라우저 쿠키는 **로그인부터 48시간(2일)** 유효하며 요청마다 연장하지 않습니다. 로그아웃하면 즉시 무효화합니다. 기존 코드·세션의 만료 기록은 정책 변경으로 소급 변경하지 않습니다. 로그인 코드·쿠키·CSRF 토큰은 공개 문서나 URL·로그에 넣지 마세요.

## 설정과 CLI 실행

```sh
cp config.example.yaml config.yaml
```

GUI의 프로젝트 등록·설정에서 **검증 자동 감지**를 누르면 기준 브랜치에 커밋된 설정을 읽어 Python pytest, Node scripts·Playwright, Java Maven·Gradle 검증 후보를 표시합니다. 실제 명령과 감지 근거를 확인하고 필요한 검증을 선택해 적용한 뒤 저장하세요. 감지는 명령 실행이나 의존성 설치를 하지 않으며 미커밋 설정은 사용하지 않습니다. 여러 언어·하위 프로젝트도 각각 작업 경로를 표시합니다. 감지가 어려우면 **도구 직접 선택**으로 시작하고 실행 파일·인자·JUnit·시간·허용 경로를 폼에서 수정할 수 있습니다. JSON은 고급 설정에 있습니다.

실행 미리보기는 저장된 pytest·Node·Maven·Gradle 정책과 감지된 도구가 다르면 설정 변경 위치를 안내합니다. 확인 가능한 JUnit 생성 옵션 누락·생성 경로와 수집 경로 불일치·출력 허용 경로 누락도 안내합니다. 저장된 정책을 자동 교체하지 않으며, 수정한 프로젝트 정책은 새 Run에 적용됩니다. 사용자 정의 명령의 의미까지 자동 판별하지는 않습니다.

Node는 실제 등록된 scripts만 추천하며 npm·pnpm·Yarn을 구분합니다. 별도 reporter·생명주기 훅·`.npmrc` 설정이 없는 단일 npm `node --test` 스크립트에는 npm의 `--node-options`로 콘솔 결과와 JUnit 파일 출력을 함께 설정합니다. 테스트 파일 뒤에 Node 옵션을 덧붙이지 않으며 원래 스크립트와 파일 선택 범위를 유지합니다. 이미 JUnit 파일 출력을 설정한 Node 스크립트는 실제 경로를 인식해 하위 프로젝트의 수집 경로와 출력 디렉터리를 맞춥니다.

Node 검증 명령이 있는 Run은 모델 작업 전에 실행용 Worktree의 의존성을 확인합니다. npm 프로젝트는 `package-lock.json`을 기준으로 `npm ci --offline --ignore-scripts --include=dev --include=optional`을 실행합니다. 로컬 캐시에 없는 공개 패키지는 호스트가 공식 npm·Yarn 레지스트리에서 받아 잠금 파일의 무결성을 확인한 뒤 오프라인 캐시에 넣습니다. 설치 프로세스의 외부 네트워크·설치 스크립트는 차단하고 해당 Worktree의 의존성 디렉터리에만 쓰기를 허용합니다. npm workspaces도 지원하며 원본 프로젝트의 `node_modules`를 공유하지 않습니다. 자동 준비한 의존성은 manifest 변경이나 설치 실패 후 다시 준비하고, 수동으로 준비한 기존 디렉터리는 검사만 합니다.

Yarn Classic 1.x가 설치된 호스트에서는 `yarn.lock`을 감지해 공개 패키지로 실행 전용 오프라인 미러를 구성하고 `--offline --frozen-lockfile --ignore-scripts`로 설치합니다. 공개 GitHub 의존성은 전체 40자리 커밋 ID로 고정된 공식 아카이브만 사용하며 아카이브의 커밋 메타데이터를 확인합니다. GitHub 아카이브에 잠금 파일의 무결성 값이 없다면 레지스트리 패키지처럼 바이트 체크섬이 고정된 것은 아닙니다. 개인 인증·사용자별 Yarn 설정은 가져오지 않습니다. 비공개 의존성·Yarn Berry·Yarn workspaces·pnpm 및 설치 스크립트가 필요한 패키지는 별도 준비가 필요합니다.

Vite 설정 파일이 있는 빌드 후보에는 `node_modules/.vite-temp`를, Vue CLI 빌드에는 `node_modules/.cache`를 임시 출력 경로로 제안합니다. 새 Run 생성 시 선택한 명령과 기준 커밋을 비교해 필요한 경로를 보완합니다. Vue CLI 빌드가 사용하는 `eslint-webpack-plugin`은 호스트 실행 어댑터로 자동 수정·캐시 쓰기를 끄며 lint 오류 검사는 유지합니다. `--fix`·`--write`가 붙은 스크립트는 자동 검증 후보에서 제외합니다. `node_modules` 전체에 대한 테스트 쓰기 권한과 기존 Run의 고정 정책은 확대하지 않습니다.

macOS에서 기본 `PLAYWRIGHT_BROWSERS_PATH`를 사용하는 Playwright 검증은 npm 의존성 준비 후 설치된 `playwright-core`의 안정 버전 `1.x`에 맞는 브라우저를 자동 준비합니다. 공식 npm 레지스트리에서 같은 버전의 설치 도구를 받아 SHA-512 무결성을 확인하고, 프로젝트·사용자 파일 읽기를 허용하지 않는 별도 설치 샌드박스에서 실행합니다. 최초 준비에는 외부 네트워크가 필요합니다. 설치 도구와 브라우저는 기본 Playwright 캐시에 보관해 프로젝트 간 재사용하며 버전별 브라우저를 자동 삭제하지 않습니다. 테스트 자체의 네트워크·쓰기 권한은 유지합니다. 사용자 지정 브라우저 경로와 `PLAYWRIGHT_BROWSERS_PATH=0`은 기존 수동 준비 방식을 유지합니다.

브라우저 실행에는 Chromium의 전원 상태 알림에 필요한 IOKit 클라이언트 하나만 허용하며, 임의의 IOKit 접근은 차단합니다. Chromium 전용 `MAC_CHROMIUM_TMPDIR`도 검증용 임시 디렉터리로 지정해 다운로드가 사용자 임시 경로 쓰기 때문에 취소되지 않도록 합니다. 검증 프로세스는 자신과 자식 프로세스에만 종료 신호를 보낼 수 있어 서버·브라우저를 정상 종료하면서 다른 프로세스는 보호합니다.

잠금 파일 누락·의존성 검사 실패·다운로드 실패는 모델 작업 전에 `needs_input`으로 중단하며 원인을 표시합니다. 실행 디렉터리의 `preflight/dependencies-*-install.log`·`dependencies-*-check.log`·`yarn-*-install.log`·`COMMAND_ID-browsers.log`에서 상세 내용을 확인하세요. 실패 원인을 해결한 뒤 재개하면 준비를 다시 시도합니다. 사용자 지정 브라우저 디렉터리가 없으면 미리 중단합니다. 원본 프로젝트에만 설치한 의존성은 새 Worktree에 자동으로 전달되지 않습니다.

Vitest 스크립트에는 단발 실행·JUnit 인자를 제안하고 Playwright에는 JUnit 출력 파일을 지정합니다. 단일 Jest 스크립트는 CI 모드의 실제 JSON 결과를 수집해 테스트별 성공·실패·건너뛰기와 집계 수치를 확인한 뒤 JUnit으로 변환합니다. 복합 스크립트·별도 npm 설정·생명주기 훅·자동 설정이 어려운 조합은 필요한 보고서 옵션과 경로를 안내합니다. 추적 소스에 대한 쓰기는 허용하지 않습니다. Java는 Wrapper가 있으면 우선 사용하며 Maven Surefire·Gradle의 파일별 `TEST-*.xml` 보고서를 수집합니다. 사용자 정의 테스트 태스크·멀티모듈 설정은 실제 명령과 출력 경로를 확인하세요.

Java 하위 모듈은 상위 디렉터리의 `mvnw`·`gradlew`도 찾아 사용합니다. 새 Run을 만들 때 Maven·Gradle 명령에 누락된 오프라인 옵션, Worktree 내부 캐시 경로와 출력 권한을 보완합니다. 커밋된 POM의 Java 버전 속성이나 Gradle의 리터럴 toolchain·`sourceCompatibility` 선언을 읽어 설치된 JDK를 선택하고 `JAVA_HOME`을 실행 설정에 저장합니다. 명시한 `JAVA_HOME`은 교체하지 않습니다. Gradle Wrapper 버전의 JVM 호환성도 확인하며, 컴파일 toolchain과 실행 JVM이 다르면 설치된 JDK 경로를 Gradle에 전달합니다. JDK를 자동 다운로드하거나 프로젝트의 빌드 파일을 수정하지는 않습니다.

실행 전 Maven은 로컬 `~/.m2/repository`와 해당 버전의 Wrapper 배포본을, Gradle은 `~/.gradle/caches/modules-2`와 해당 버전의 Wrapper 배포본을 격리된 Worktree 캐시에 복사합니다. 호스트의 `MAVEN_USER_HOME`·`GRADLE_USER_HOME`을 지정했다면 그 아래 캐시를 사용합니다. 원본 캐시와 쓰기 가능한 파일을 공유하지 않으며 기존 실행 캐시 파일도 덮어쓰지 않습니다. 인증 설정·사용자 Gradle 초기화 스크립트·잠금 파일·`gc.properties`·심볼릭 링크는 복사하지 않습니다. Gradle 캐시 복사 범위와 버전별 호환성은 [공식 캐시 문서](https://docs.gradle.org/current/userguide/dependency_caching.html), JVM 호환성은 [공식 호환성 표](https://docs.gradle.org/current/userguide/compatibility.html)를 기준으로 합니다.

실행 비트가 없는 POSIX Wrapper는 `/bin/sh`로 실행하며 원본 파일 권한을 바꾸지 않습니다. 루트에 `settings.gradle`만 있는 멀티모듈도 새 Run에 루트 `build` 출력 경로를 포함합니다. Gradle 테스트의 Mockito는 실제 클래스패스에 있는 Mockito 또는 Byte Buddy 에이전트를 JVM 시작 시 연결해 샌드박스의 동적 자기 연결 제한을 유지합니다. 실행 어댑터의 해시와 실제 실행 인자는 검증 결과에 기록합니다.

Gradle에 리터럴 버전으로 선언된 `com.google.protobuf:protoc`·`io.grpc:protoc-gen-grpc-java`는 공식 Maven 저장소의 체크섬을 확인해 격리 캐시에 준비하고 호스트 초기화 스크립트로 실행 경로를 전달합니다. 기존 Gradle 검증 메타데이터가 있으면 해당 바이너리의 체크섬을 요구하며 우회하지 않습니다. 프로젝트의 저장소·빌드 파일은 변경하지 않습니다. 배포본의 실제 CPU 아키텍처와 호스트가 다르면 별도의 호환 실행 환경이 필요합니다. macOS에서 일부 과거 gRPC 배포본은 ARM 분류자에도 x86_64 바이너리가 들어 있어 Rosetta 또는 호환 버전이 필요할 수 있으며 자동으로 설치하거나 버전을 교체하지 않습니다.

JDK·빌드 도구·Wrapper 준비 실패와 명시적으로 확인 가능한 의존성 캐시 누락은 모델 호출 전에 `needs_input`으로 중단합니다. `preflight/COMMAND_ID-jdk.log`·`COMMAND_ID-tool.log`에서 실행된 버전 점검 결과를 확인할 수 있습니다. 상위 POM·BOM·버전 카탈로그·동적 빌드 스크립트를 모두 해석하는 의존성 해결기는 아니므로 간접 의존성·플러그인 누락은 실제 빌드에서 발견될 수 있습니다. 필요한 JDK·배포본·의존성이 로컬에 없으면 호스트 또는 해당 Run의 격리 캐시에 준비한 뒤 재개해야 합니다. 외부 저장소 접근을 위해 프로젝트 빌드를 네트워크 권한으로 실행하거나 개인 인증 설정을 가져오지 않습니다. DB·Redis·Docker 같은 통합 테스트 서비스도 자동 기동하거나 접근 권한을 확대하지 않습니다. 기존 Run의 고정된 명령·출력 권한은 유지하므로 캐시 옵션이 빠진 기존 Run은 새로 생성해야 합니다.

CLI에서는 설정의 `tests.commands`를 대상 프로젝트에 맞게 수정한 뒤 실행합니다. 별도 설정 파일을 전달하지 않은 내장 기본값에는 테스트 명령이 없으므로 작업 실행 전에 설정이 필요합니다.

- `tests.commands`: 호스트가 신뢰하는 명령 ID와 `argv` 인자 배열, 제한 시간을 지정합니다. `kind: test`(기본값)는 단일 파일 또는 glob인 `junit_report`에 매번 새 JUnit XML을 생성해야 합니다. `kind: check`는 빌드·타입·정적 검사를 종료 코드로 판정하며 JUnit을 지정하지 않습니다. 테스트 명령은 반드시 하나 이상 있어야 하고 모든 선택 명령이 통과해야 합니다.
- `working_directory`: 명령을 실행할 저장소 기준 상대 경로이며 기본값은 `.`입니다. `junit_report`, `tests.write_paths`, `tests.output_paths`는 작업 경로와 관계없이 저장소 루트를 기준으로 지정합니다.
- `network`: 기본값 `none`은 네트워크를 차단합니다. macOS의 `loopback`은 브라우저 검사·Gradle 내부 통신에 필요한 localhost 통신을 허용하고 외부 네트워크는 차단합니다. 서버 주소는 `127.0.0.1`을 사용하세요. Linux의 loopback 모드는 모델 호출 전에 실행을 거부하며 일반 오프라인 검증은 기존 격리를 유지합니다.
- `env`: 명령별 `JAVA_HOME`, `PLAYWRIGHT_JUNIT_OUTPUT_FILE`, `PLAYWRIGHT_BROWSERS_PATH`만 지정할 수 있습니다. Java는 설치된 JDK와 로컬 의존성·Wrapper 캐시를 자동 선택·복사하며, 로컬에 없는 항목은 미리 준비해야 합니다. 사용자 지정 경로의 브라우저는 수동 준비하고 macOS 기본 경로의 Playwright 브라우저는 자동 준비합니다. Java 실행 캐시는 Worktree의 `.devorchestra-cache`를 사용합니다.
- `tests.write_paths`: 테스트 파일을 추가·수정할 수 있는 경로이며 기존 테스트 변경 근거의 검증 범위입니다. 기존 파일에 내용을 추가하는 경우도 변경 사유와 인수 조건 ID를 검증합니다.
- `tests.output_paths`: 테스트 격리 중 쓰기를 허용하는 빌드·보고서 경로입니다. Git이 추적하는 소스 파일이 포함된 디렉터리는 허용하지 않습니다.
- `storage`: 기본 위치는 저장소 외부의 `~/.devorchestra`입니다. DB·실행 산출물·Worktree를 대상 저장소 내부에 두면 실행을 거부합니다. 상대 저장 경로는 YAML 파일 위치를 기준으로 해석합니다.
- `codex.model`·`codex.reasoning_effort`: 공통 모델과 추론 강도입니다. `codex.roles`의 `planner`·`developer`·`tester`·`reviewer`·`fixer`에서 각각 `model`·`reasoning_effort`를 재정의하며, `null`은 공통값을 상속합니다. 공통값도 `null`이면 Codex 기본값을 사용합니다. 실행은 `--ignore-user-config`를 사용하므로 개인 Codex 설정 파일의 값을 상속하지 않습니다.

GUI의 **설정 → 에이전트 모델 설정**에서 **모델 목록 불러오기**를 누르면 설치된 Codex의 공식 `model/list` 조회로 모델과 지원 추론 강도를 선택할 수 있습니다. 이 조회는 모델 작업을 시작하지 않습니다. 추론 강도를 명시하려면 모델도 지정해야 하며, 역할이 상속한 공통 추론 강도도 해당 모델에서 지원해야 합니다.

UI에서 저장한 설정은 DB 옆의 `.agents.json` 확장자 파일에 보관하고 같은 DB를 쓰는 GUI·CLI의 새 실행에 적용합니다. YAML의 Codex 명령·제한 시간은 유지합니다. 저장 전에는 YAML의 모델 설정을 사용하며, 저장 후에는 UI 설정이 우선합니다. 진행 중이거나 과거 실행은 생성 당시 스냅샷을 유지합니다. 미리보기 후 모델 설정이 바뀌면 실행 전 확인을 다시 해야 합니다.

실행 상세의 **에이전트·사용량** 탭은 역할별 설정 모델·추론 강도, 각 시도의 입력·캐시 입력·출력 토큰과 Codex 소요 시간을 표시합니다. 실패·재시도도 합계에 포함합니다. 캐시 입력은 입력 토큰의 일부이며 중복 합산하지 않습니다. 중단·구버전 실행 등으로 기록이 없는 수치는 `0` 대신 미확인으로 표시하며 일부만 수집되면 일부 집계로 표시합니다. 표시한 모델은 요청 설정이며 실제 서버 라우팅 결과를 보장하지 않습니다. 계정 전체의 잔여 사용 한도·요금은 이 집계에 포함하지 않습니다.

저장한 정책은 **새 작업**에 적용됩니다. 기존 작업은 생성 당시의 검증 계약을 보존하며, 재개 답변으로 명령이 바뀌지 않습니다. 정책 불일치가 있으면 작업 화면의 **프로젝트 검증 설정 열기**에서 수정한 뒤 **수정한 정책으로 새 작업 만들기**를 사용하세요. 이전 작업의 변경 내용과 이력은 보존되며 새 작업으로 자동 이전하지 않습니다.

테스트는 셸 명령 문자열을 해석하지 않고 외부 네트워크를 차단합니다. 필요한 의존성은 위에서 설명한 자동 준비 범위 안에서 설치하며, 그 밖의 의존성·실행 환경은 미리 준비해야 합니다. Maven·Gradle·JavaScript 도구도 JUnit XML을 생성하면 사용할 수 있지만 프로젝트별 출력 경로 설정이 필요할 수 있습니다. 테스트 0건, 전체 건너뛰기, 보고서 누락·형식 오류를 통과로 처리하지 않습니다. 보고서 누락 등으로 멈추면 실패한 명령 ID·상태·종료 코드와 진단 출력을 함께 표시합니다.

`--config`·`--database`는 하위 명령 앞에 지정합니다. 아래 예시는 모든 명령에서 같은 설정을 사용합니다.

```sh
devorchestra --config config.yaml doctor
devorchestra --config config.yaml run --repo ~/projects/example --spec PRD.md --base main --max-fix-attempts 3
# CLI 종료 후에도 실행하려면 run에 --detach를 지정합니다.
devorchestra --config config.yaml status RUN_ID
devorchestra --config config.yaml list
devorchestra --config config.yaml logs RUN_ID --follow --role reviewer
devorchestra --config config.yaml pause RUN_ID
devorchestra --config config.yaml resume RUN_ID --answers answers.md --max-fix-attempts 5
devorchestra --config config.yaml cancel RUN_ID
devorchestra --config config.yaml report RUN_ID
devorchestra --config config.yaml approve RUN_ID
devorchestra --config config.yaml cleanup RUN_ID
```

`doctor`는 모델 호출 없이 Codex 명령 호환성과 OS 테스트 샌드박스를 확인합니다. 조회 명령은 JSON을 출력하며 `logs`는 로그, `report`는 보고서 본문을 출력합니다. 일반 실행에서 `Ctrl+C`는 안전한 일시중지를 요청합니다. 취소는 하위 프로세스 종료를 확인한 뒤 확정합니다.

`--detach`를 생략하면 Dispatcher가 먼저 시작한 기존 Worker도 해당 실행 시도가 끝날 때까지 기다리며, 실패·추가 입력 필요 결과를 종료 코드에 반영합니다.

| 상황 | 종료 코드 |
|---|---|
| 일반 완료·최종 승인 대기 도달 | `0` |
| 명령·입력 오류 | `1` |
| 포그라운드 `run`·`resume`의 실행 실패 또는 사용자 개입 필요 | `2` |

## 워크플로와 승인

```text
queued → preparing → planning → waiting_plan_approval → developing → testing → reviewing
                                                         ↓
                        waiting_approval ← reporting ← 검증 통과
```

테스트 실패나 차단 리뷰 이슈가 있으면 `fixing → testing → reviewing`을 반복합니다. 자동 수정은 기본 최대 3회이며, `needs_input`은 질문 답변·수정 한도 상향 같은 사용자 판단이 필요한 상태입니다. `resume`은 검증된 체크포인트를 보존하며 수정 횟수를 초기화하지 않습니다.

최종 단계까지 마친 작업을 일시중지한 뒤 `resume --answers`로 추가 요구사항을 전달하면 새 revision에서 기획·개발·테스트·리뷰를 수행합니다. 기획 승인이 필요한 작업은 기획을 다시 승인해야 하며, 이전 최종 검증 근거는 재사용하지 않습니다. 답변 없는 재개는 유효한 기존 체크포인트를 재사용합니다.

재개할 때 입력한 답변은 기존 지시와 함께 누적되고, 해당 질문과 연결해 모든 역할에 전달됩니다. 답변한 질문은 화면에서 해제되며, 플래닝 중 작성한 잠정 계획도 다음 시도에 전달합니다. 플래너는 확정된 사항을 다시 확인하지 않고 구현을 막는 미해결 사항만 질문합니다. 변경 전에 덮어써진 과거 답변은 자동 복원되지 않으므로 필요한 합의는 다시 입력하세요.

모델 응답은 역할별 스키마로 제한합니다. Developer·Fixer가 변경할 코드가 없다면 `no_change_reason`에 이유를 반환해야 합니다. 잘못된 응답 형식, 인수 조건·명령 연결 누락, 적용할 수 없는 패치는 같은 SHA에서 해당 역할만 다시 작성합니다. `workflow.response_retries`는 단계별 응답 복구 횟수이며 기본값 1, 허용 범위 0~2입니다. 제품 코드의 자동 수정 한도 `max_fix_attempts`와 별도이고 실패 시도도 사용량에 포함됩니다. 필드가 없는 기존 Run은 기본값을 사용하며 완료된 기획·체크포인트를 유지합니다.

복구에는 구체적인 검증 오류와 필요시 추적 중인 대상 파일의 제한된 현재 문맥을 전달합니다. Worktree 변경, 허용 경로 위반, 프로세스·실행 오류는 이 자동 복구로 우회하지 않습니다. 복구 한도 초과는 `needs_input`으로 보존하고, 중단된 재시도를 다시 시작할 때도 추가 호출 전에 개입을 요청합니다. 답변 없는 단순 재개로 같은 단계의 복구 한도를 초기화하지 않습니다. 명시적으로 새 답변을 제공하면 새로운 입력으로 한도를 계산하며, 기존 기획 승인 규칙은 유지합니다. 기존 테스트 변경 근거·전체 테스트·독립 리뷰·최종 승인은 그대로 필요합니다.

각 역할은 새 읽기 전용 Codex 세션에서 구조화된 결과를 반환합니다. 호스트가 unified diff를 검증·적용합니다. 요구사항 변경으로 기존 테스트의 기대값을 수정하거나 검사를 추가해야 하면, 에이전트는 변경 사유와 인수 조건 ID를 `test_changes`로 함께 반환합니다. 호스트는 파일별 변경 근거·패치·테스트 명령·인수 조건 커버리지를 적용 전에 검증하고 중간 승인 없이 자동 진행합니다. 테스트 변경을 이유로 `needs_input`에 진입하지 않으며 전체 설정 테스트와 독립 리뷰를 거쳐 최종 승인만 받습니다.

기존 테스트 변경 근거는 Run ID·입력 SHA·revision·패치 체크섬에 연결해 보존하고 다음 역할에도 전달합니다. 독립 리뷰는 변경된 기대값이 요구사항에 맞는지와 회귀 검사 삭제·skip·검증 약화 여부를 확인합니다. GUI의 **기존 테스트 변경 내역**과 최종 보고서에서 사유·인수 조건을 확인하고 전체 diff와 테스트·리뷰 결과를 검토한 뒤 최종 승인하세요. 변경 근거가 누락되거나 손상되면 적용·최종 승인을 차단합니다.

기존 테스트 패치 적용 후 테스트가 일시정지되거나 실행 오류로 중단되면 적용 결과와 체크섬을 보존합니다. 일반 `resume`으로 같은 패치를 다시 생성·적용하지 않고 검증을 재시도합니다. 보관된 결과·현재 HEAD·정책·요구사항이 바뀌면 재사용하지 않습니다. 적용 후 검증 대기 중에는 추가 지시를 섞지 않고 먼저 검증을 완료합니다. 환경 오류나 JUnit 누락은 정상 통과로 처리하지 않으며 원인을 해결한 뒤 재개합니다.

```sh
devorchestra --config config.yaml test-changes RUN_ID
devorchestra --config config.yaml resume RUN_ID --detach
```

이전 버전에서 중간 승인 대기 중이던 제안도 일반 `resume` 시 보관한 정확한 패치와 실행 기준을 검증해 자동 적용합니다. 이전 승인을 받은 제안은 그 승인 근거도 유지합니다. 구버전 CLI·API의 `approve-test-changes`와 `reject-test-changes`는 기존 제안과의 호환성을 위해 남아 있지만 새 실행은 이 승인을 요구하지 않습니다. 보관한 제안에 추가 지시가 필요하거나 제안이 손상됐다면 `reject-test-changes RUN_ID --proposal-hash PROPOSAL_HASH --input-sha INPUT_SHA --reason "수정 지시"`로 폐기하고 다시 작성할 수 있습니다.

자동 테스트 수정에서도 테스트 삭제·skip·검증 약화나 명령 변경은 허용하지 않습니다. Test Agent는 제품 코드를 변경할 수 없습니다. 파일 삭제·이름 변경, 바이너리 패치, 심볼릭 링크, 권한 변경은 자동 적용하지 않습니다. 자동 수정 중 Planning에서 확정한 인수 조건을 바꾸지 않습니다.

승인은 현재 HEAD·작업 트리·테스트·리뷰·체크포인트와 검증 산출물의 무결성이 모두 확인된 후보에만 적용합니다. 리뷰 V2의 이슈 해결에는 같은 후보 SHA·revision의 새 테스트와 명시적 재검증 근거가 필요하며, 이전 리뷰에서 빠졌다고 자동 해결하지 않습니다. 차단 이슈는 사용자 분류로 우회할 수 없습니다.

웹용 스키마 5에서는 승인 대기 중 수정 요청과 승인 보류를 CLI에서도 사용할 수 있습니다.

```sh
devorchestra --config config.yaml request-changes RUN_ID --instructions changes.md --detach
devorchestra --config config.yaml defer RUN_ID --reason "추가 확인 후 승인"
```

수정 요청은 새 revision에서 Fix·Test·Review를 다시 수행하며 변경이 없는 Fix도 새 검증을 거칩니다. 수정 한도는 유지하고 필요하면 `--new-max-fix-attempts`로 상향합니다. 승인은 특정 SHA의 이력이며, 이후 변경된 승인 결과는 `status`에서 사용자 개입이 필요한 상태로 표시합니다.

`cleanup`은 Worktree만 정리하고 브랜치·커밋·보고서·SQLite 이력을 보존합니다. 미커밋 변경이 있는 Worktree를 강제로 삭제하지 않습니다. 보존된 Diff·JUnit·보고서는 Worktree 정리 후에도 조회할 수 있습니다.

CLI·GUI 어느 쪽에서 정리해도 해당 작업은 재개할 수 없습니다. 계속 개발하려면 새 Run을 생성하세요. 제거 후 상태 기록이 중단됐을 때는 `cleanup`을 다시 실행해 기록을 완료할 수 있습니다.

완료·취소된 작업은 GUI 상세 화면의 **작업 기록 삭제** 또는 `devorchestra --config config.yaml delete RUN_ID`로 영구 삭제할 수 있습니다. CLI는 삭제 범위를 확인하며 `--yes`로 확인을 생략할 수 있습니다. 실패한 작업은 먼저 `cancel RUN_ID`로 취소해야 합니다. 삭제 범위에는 실행 단계·저장된 요구사항·로그·테스트 및 리뷰 결과·보고서·승인 근거·큐와 명령 이력·설정된 JUnit 결과 파일·Worktree·로컬 작업 브랜치가 포함됩니다. 미병합 작업 브랜치도 삭제하므로 필요한 커밋은 미리 보존하세요. 원본 작업 디렉터리와 원격 브랜치는 보존합니다. 다른 작업에서 참조하는 요구사항은 보존합니다.

삭제는 Worker 소유권·실행 잠금·미처리 큐와 명령·자식 프로세스 종료를 확인합니다. 작업에 저장된 JUnit 경로와 출력 경계 안의 추적되지 않는 결과 파일만 자동 정리하며, 그 외 미커밋 변경이 있는 Worktree를 강제로 제거하지 않습니다. 브랜치가 마지막 작업 커밋과 다르거나 기준 브랜치·다른 작업에서 참조하는 브랜치·다른 Worktree에서 사용 중인 브랜치이면 삭제를 중단합니다. 파일 삭제가 중단되면 일부 파일이 이미 삭제되었을 수 있습니다. 삭제 진행 상태는 DB에 보존되며 다른 변경 명령을 차단합니다. 원인을 해결한 뒤 GUI의 **기록 삭제 재시도** 또는 같은 CLI 명령으로 재시도할 수 있습니다. API는 `DELETE /api/runs/RUN_ID`에 최신 `If-Match`와 `{"confirm": true}`를 요구합니다. 별도로 보관한 백업·다운로드 파일과 Git 커밋에 남은 내용은 이 작업 기록 삭제의 대상이 아닙니다.

### 기획 승인

신규 작업은 기획 완료 후 **기획 승인 대기**에서 멈춥니다. GUI의 **기획안** 탭에서 구현 계획과 인수 조건을 검토한 뒤 **기획 승인 후 개발 시작**을 선택하세요. **기획 수정 요청**은 사유를 반영해 새 기획을 생성하며 다시 승인을 받습니다. 보류하려면 승인 대기 상태를 유지하면 됩니다. 기존 작업은 생성 당시 정책을 유지합니다. 최종 개발 결과에 대한 승인은 별도로 필요합니다.

CLI에서는 `status RUN_ID`의 기획안과 인수 조건을 확인하고 다음 명령을 사용합니다. `PLAN_HASH`는 GUI의 기획 버전 또는 기획 승인 대기 상태 조회의 `plan_hash`입니다.

```sh
devorchestra --config config.yaml approve-plan RUN_ID --plan-hash PLAN_HASH
devorchestra --config config.yaml revise-plan RUN_ID --plan-hash PLAN_HASH --reason "예외 처리 계획 보완"
```

일반 `resume`은 기획 승인을 대신하지 않습니다. 승인 시 기획 내용·입력 SHA·저장된 결과의 체크섬을 검증하며, 수정 요청이나 기획 내용 변경 후에는 이전 승인을 사용할 수 없습니다.


## API·Dispatcher와 GUI 개발

CLI 기본 DB는 스키마 2를 사용하며 웹·큐는 스키마 5가 필요합니다. API 시작만으로 기존 DB를 자동 업그레이드하지 않습니다. 아래 명령은 같은 `config.yaml`을 공유합니다.

```sh
# 최초 웹 사용 또는 구버전 DB 업그레이드 시 실행합니다.
devorchestra --config config.yaml database-upgrade

# API와 GUI 개발 서버는 각각 별도 터미널에서 실행합니다.
devorchestra --config config.yaml api-start --allow-root ~/projects
devorchestra --config config.yaml worker start --detach
devorchestra --config config.yaml web-session

npm ci
npm run dev:web
```

마이그레이션 전에 API·웹 서비스·Dispatcher·Run Worker 등 DB writer를 중지하세요. 실행 중 Worker·미확인 프로세스·잠금이 있으면 거부하며, 스키마 2·3·4의 SQLite 백업과 증거 참조 manifest를 DB 옆에 보존한 뒤 트랜잭션으로 업그레이드합니다. 구버전 실행 파일과 혼용하지 말고 복원도 모든 writer가 종료된 상태에서 수행하세요.

API 기본 주소는 `http://127.0.0.1:8000`, Vite GUI는 `http://127.0.0.1:5173`입니다. `localhost`로 바꾸지 마세요. API 포트를 바꿨다면 `DEVORCHESTRA_API_PORT=8001 npm run dev:web`처럼 지정합니다. API는 loopback에만 바인딩하고 세션·정확한 Origin/Host·쓰기 CSRF를 검사합니다. 프로젝트는 `--allow-root` 아래 Git 저장소만 등록할 수 있으며 등록 해제는 원본 저장소를 삭제하지 않습니다.

API는 작업을 `queued`로 접수하고 독립 Dispatcher가 실행합니다. Dispatcher가 없으면 `queued_reason=dispatcher_offline`, 다른 작업이 실행 중이면 `waiting_capacity`를 반환합니다. `worker status`로 확인하고 `worker stop`으로 새 작업 실행을 중지할 수 있습니다. Dispatcher 종료와 API·SSE 연결 종료는 실행 중 Run 취소를 의미하지 않습니다. 미등록 CLI 작업은 GUI에서 조회 전용입니다.

SSE는 영속 event ID로 재연결·재생하며 GUI는 연결 단절 중 이력을 주기적으로 조회합니다. 로그 화면은 가상 목록과 최대 1,000개 구간·1 MiB 보관 한도를 사용합니다. 화면 로그 저장은 현재 보관 구간이며 전체 로그는 별도 다운로드를 사용합니다. 인증된 `/api/openapi.json`·`/api/docs`는 OpenAPI JSON을 제공하고 외부 CDN의 Swagger UI는 사용하지 않습니다. 화면과 프론트엔드 세부 사항은 [웹 앱 안내](apps/web/README.md)를 참고하세요.

Dispatcher는 큐 선점 후 파일 수·시간 예산 안에서 로그를 색인합니다. 종료 로그의 처리 완료와 순회 위치는 Run 디렉터리에 기록하며, 완료된 이력은 매 회차 전체 로그를 열지 않고 순서대로 재확인합니다. 종료 후 추가·교체·축소된 로그는 완료 후 최소 30초가 지난 재확인 차례에 반영되므로 이력 수와 미처리량에 따라 반영이 늦어질 수 있습니다.

## 정적 GUI와 배포

```sh
npm ci
.venv/bin/python scripts/build_web.py
uv build
```

빌드 도구는 `apps/web/dist/`를 `src/dev_orchestra/_web/`로 복사하고 체크섬 manifest를 생성합니다. GUI가 포함된 wheel을 설치한 환경에서는 Node 없이 Python의 `web` extra로 실행할 수 있습니다. 소스 저장소의 `start.sh`는 매번 빌드하므로 Node가 필요하고, 설치된 패키지는 다음 통합 명령을 사용합니다.

```sh
devorchestra --config config.yaml database-upgrade
devorchestra --config config.yaml web start --detach --allow-root ~/projects --port 8000
devorchestra --config config.yaml web-session
devorchestra --config config.yaml web status
devorchestra --config config.yaml web stop
```

GUI와 API를 `http://127.0.0.1:8000`에서 함께 제공합니다. GUI 파일이 없는 CLI 전용 배포도 지원하며 `web start`에서 빌드 필요성을 안내합니다. `web start`는 정적 파일 체크섬·포트·실행 소유권을 확인합니다. `web stop --cancel-active`는 같은 DB의 미완료 작업 취소까지 요청합니다.

## 저장소 구조

| 경로 | 역할 |
|---|---|
| `src/dev_orchestra/domain/`, `src/dev_orchestra/config.py` | 도메인 모델·설정 검증 |
| `src/dev_orchestra/application/` | CLI·API 공통 실행·조회·프로젝트·큐·결과·승인 서비스 |
| `src/dev_orchestra/orchestration/`, `src/dev_orchestra/agents/` | 워크플로 실행·역할 프롬프트·패치 정책 |
| `src/dev_orchestra/infrastructure/` | Git·SQLite·샌드박스·프로세스 관리·검증 근거 |
| `src/dev_orchestra/cli.py`, `src/dev_orchestra/api/`, `src/dev_orchestra/execution/`, `src/dev_orchestra/worker.py` | CLI·HTTP·SSE 어댑터와 독립 실행 프로세스 |
| `apps/web/` | React + TypeScript + Vite GUI |
| `tests/`, `apps/web/tests/`, `apps/web/e2e/` | Python·화면·브라우저 검사 |
| `scripts/` | GUI 빌드·API 생성·배포 검사·벤치마크·선택 E2E |
| `start.sh`, `stop.sh` | 소스 저장소의 반복 기동·종료 |

공통 서비스는 FastAPI·Typer에 의존하지 않습니다. 읽기 전용 조회는 복구·보고서 생성을 수행하지 않으며 상태 조정은 별도 경로로 처리합니다. 기존 CLI `status`는 복구 동작을 유지합니다. 기여 시에는 [저장소 작업 규칙](AGENTS.md)을 따르세요.

## 개발 및 검증

저장소 루트에서 웹 개발 환경과 npm 의존성을 준비한 뒤 필요한 범위의 검사를 실행합니다. 아래 일반 검사는 실제 모델을 호출하지 않습니다.

```sh
.venv/bin/python -m pytest -q
.venv/bin/ruff check src tests scripts apps/web/e2e/backend.py
.venv/bin/ruff format --check src tests scripts apps/web/e2e/backend.py
npm run test:web
npm run typecheck:web
npm run format:check --workspace apps/web

# 공개 API 계약과 생성 타입의 일치 확인
.venv/bin/python scripts/export_web_schema.py --check
npm run check:api --workspace apps/web

# 실제 OS 샌드박스 검사와 로그 재생 벤치마크
DEVORCHESTRA_TEST_SANDBOX=1 .venv/bin/python -m pytest tests/test_security.py -q

# macOS: 설치된 Chromium으로 실제 브라우저 격리·JUnit 수집 검사 (선택)
DEVORCHESTRA_TEST_BROWSER_SANDBOX=1 .venv/bin/python -m pytest tests/test_validation_policy.py -k actual_playwright -q
.venv/bin/python scripts/benchmark_logs.py
```

API 계약을 변경했다면 `.venv/bin/python scripts/export_web_schema.py`와 `npm run generate:api --workspace apps/web`을 차례로 실행해 OpenAPI와 TypeScript 타입을 함께 갱신합니다.

```sh
# 실제 브라우저·임시 Git/SQLite·Mock Codex·실제 pytest
npx playwright install chromium --only-shell
npm run test:e2e --workspace apps/web

# Python이 제공하는 정적 GUI에서도 같은 흐름 검사
.venv/bin/python scripts/build_web.py
E2E_BUNDLED=1 npm run test:e2e --workspace apps/web
```

브라우저 검사는 5173·18080 포트를 사용하며 기존 서버를 재사용하지 않습니다. 테스트 상태는 임시 저장소·DB에 격리합니다. 검사 산출물과 실패 trace에는 테스트용 세션 정보가 포함될 수 있으므로 외부 공유 전 확인하세요.

wheel을 각각 설치한 웹 환경(`web` extra 포함)과 CLI 전용 환경을 준비했다면 다음 검사로 Node 없는 GUI와 웹 의존성 없는 CLI를 확인할 수 있습니다.

```sh
.venv/bin/python scripts/verify_package.py --web-python /absolute/path/to/web-env/bin/python --core-python /absolute/path/to/core-env/bin/python
```

다음 E2E는 실제 Codex를 호출하고 사용량이 발생합니다. 사용자 승인 후 선택 실행하며 일반 테스트·CI에서 자동 실행하지 마세요.

```sh
.venv/bin/python scripts/e2e.py
.venv/bin/python scripts/e2e_gui.py --allow-model-calls --model MODEL_ID
```

GUI 모델 검사는 Developer 응답에 명시적인 결함을 주입하고 원래 응답을 보존한 뒤 Fix·Test·Reviewer V2 재검증·CLI 승인을 확인합니다. 실행당 최대 7회 호출, 역할별 제한 시간 180초를 사용합니다. 개별 테스트 통과는 지원 범위 밖 환경이나 장시간 장애 복구까지 보장하지 않습니다.

## 안전성과 운영 제약

- **원본과 Git**: 원래 작업 디렉터리의 수정 내용·현재 브랜치를 보존하고 기준 커밋에서 Worktree를 만듭니다. 호스트 Git 명령은 hooks·서명·fsmonitor를 비활성화합니다. 실행 가능한 clean/smudge/process 필터가 있는 저장소는 거부하므로 Git LFS 프로젝트도 해당할 수 있습니다.
- **샌드박스**: 테스트 소스는 읽기 전용이며 지정 출력 경로·임시 HOME·선별 환경변수를 사용합니다. macOS는 알려진 인증 경로·프로젝트의 환경 파일·키 읽기를 차단하지만 모든 호스트 비밀 파일을 격리하지는 못합니다. Codex 읽기 전용 세션도 관련 없는 모든 호스트 파일의 읽기까지 막지는 않으므로 신뢰할 수 없는 저장소는 전용 계정·컨테이너에서 실행하세요.
- **복구와 소유권**: Worker 상실 시 별도 관리 프로세스가 자식 명령을 정리합니다. PID·생성 시각·실행 잠금으로 소유권을 확인하고 디스크의 PID만으로 다른 프로세스를 종료하지 않습니다. 종료 확인 실패, 외부 HEAD 변경, 미커밋 변경, 커밋 직후 체크포인트 누락은 수동 확인이 필요합니다. 자동 reset·clean으로 복구하지 않습니다.
- **동시성**: SQLite는 WAL·외래 키·짧은 트랜잭션을 사용하고 DB별 Worker 하나만 실행합니다. 다른 DB가 같은 Git common directory를 사용해도 저장소 잠금으로 직렬화합니다. heartbeat 지연만으로 살아 있는 Worker를 재실행하지 않습니다.
- **검증의 한계**: PRD·저장소 파일은 호스트 명령 선택이나 승인을 바꿀 수 없습니다. 프롬프트 인젝션이나 테스트 의미 약화를 완전히 자동 탐지할 수는 없고 모델의 요구사항 대응표도 충족의 증명은 아닙니다. 승인 후 파일시스템 감시는 없으므로 `status`로 유효성을 확인하세요.
- **민감정보 보관**: 로그는 마스킹하며 프로세스 출력은 최대 8 MiB입니다. 모든 비밀 형식을 식별할 수 없고 PRD·설정·구조화 응답·원본 진단 로그에는 프로젝트 정보가 남을 수 있습니다. 기본 상태 디렉터리 `~/.devorchestra`를 비공개로 관리하고 로그인 코드·세션·키·DB·로그를 커밋하지 마세요.
- **산출물과 전원**: 실행 자료는 설정된 `runs_directory`의 `RUN_ID/iterations/ITERATION/ROLE/attempt-NNN/`에 저장하고 실행 루트에 스냅샷·`final-report.md`를 보존합니다. 장시간 작업 중에는 컴퓨터가 실행 가능한 상태를 유지해야 하며 절전·세션 중단 뒤 모든 복구가 검증된 것은 아닙니다.
