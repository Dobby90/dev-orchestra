import { useState, type FormEvent } from "react";
import type { Project, Schema } from "../api/types";
import { Button } from "./ui/button";
import { Modal } from "./ui/dialog";
import { ValidationEditor } from "./ValidationEditor";
export const initialPolicy = {
  tests: {
    commands: {
      unit: {
        kind: "test" as const,
        working_directory: ".",
        network: "none" as const,
        argv: [
          "python",
          "-m",
          "pytest",
          "tests",
          "--junitxml=build/junit.xml",
          "-q",
        ],
        timeout_seconds: 600,
        junit_report: "build/junit.xml",
      },
    },
    write_paths: ["tests"],
    output_paths: ["build", ".pytest_cache"],
  },
  max_fix_attempts: 3,
  blocking_severities: ["critical", "high"],
};
function editablePolicy(value: unknown): value is Schema["Policy"] {
  if (!value || typeof value !== "object") return false;
  const p = value as Record<string, unknown>;
  if (
    !p.tests ||
    typeof p.tests !== "object" ||
    !Array.isArray(p.blocking_severities)
  )
    return false;
  const tests = p.tests as Record<string, unknown>;
  const strings = (v: unknown) =>
    v === undefined ||
    (Array.isArray(v) && v.every((x) => typeof x === "string"));
  if (
    !strings(tests.write_paths) ||
    !strings(tests.output_paths) ||
    !strings(p.blocking_severities)
  )
    return false;
  if (tests.commands === undefined) return true;
  if (
    !tests.commands ||
    typeof tests.commands !== "object" ||
    Array.isArray(tests.commands)
  )
    return false;
  return Object.values(tests.commands).every((value) => {
    if (!value || typeof value !== "object") return false;
    const c = value as Record<string, unknown>;
    return (
      Array.isArray(c.argv) &&
      c.argv.every((arg) => typeof arg === "string") &&
      (c.env === undefined ||
        (!!c.env &&
          typeof c.env === "object" &&
          Object.values(c.env).every((v) => typeof v === "string")))
    );
  });
}
export function ProjectForm({
  project,
  formId,
  pending,
  onSubmit,
}: {
  project?: Project;
  formId?: string;
  pending: boolean;
  onSubmit: (body: Schema["ProjectCreate"]) => void;
}) {
  const [guideOpen, setGuideOpen] = useState(false);
  const [name, setName] = useState(project?.name ?? ""),
    [path, setPath] = useState(project?.repository_path ?? ""),
    [base, setBase] = useState(project?.default_base_branch ?? "main"),
    [policy, setPolicy] = useState(
      JSON.stringify(
        project?.policy ?? {
          ...initialPolicy,
          tests: { commands: {}, write_paths: [], output_paths: [] },
        },
        null,
        2,
      ),
    ),
    [error, setError] = useState("");
  let parsed: Schema["Policy"] | null = null;
  try {
    const value: unknown = JSON.parse(policy);
    if (editablePolicy(value)) parsed = value;
  } catch {
    /* JSON errors are shown at submit. */
  }
  function changePolicy(patch: Partial<Schema["Policy"]>) {
    if (parsed) setPolicy(JSON.stringify({ ...parsed, ...patch }, null, 2));
  }
  function submit(event: FormEvent) {
    event.preventDefault();
    try {
      const value = JSON.parse(policy);
      if (!editablePolicy(value)) throw new Error();
      if (
        !Object.values(value.tests.commands ?? {}).some(
          (c) => ((c as Schema["TestCommand"]).kind ?? "test") === "test",
        )
      ) {
        setError(
          "테스트 검증을 하나 이상 선택해 주세요. 빌드 검사만으로는 실행할 수 없습니다.",
        );
        return;
      }
      for (const command of Object.values(value.tests.commands ?? {})) {
        command.argv = command.argv.filter(
          (arg, index) => index === 0 || arg !== "",
        );
      }
      setError("");
      onSubmit({
        name,
        repository_path: path,
        default_base_branch: base,
        policy: value,
      });
    } catch {
      setError(
        "정책에 유효한 JSON 객체와 tests·blocking_severities를 입력해 주세요.",
      );
    }
  }
  return (
    <form id={formId} onSubmit={submit} className="form-grid project-form">
      <label>
        프로젝트 이름
        <input
          required
          maxLength={100}
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
      </label>
      <label>
        저장소 절대 경로
        <input
          required
          readOnly={!!project}
          value={path}
          maxLength={4096}
          placeholder="/absolute/path/to/project"
          onChange={(e) => setPath(e.target.value)}
        />
      </label>
      <label>
        기본 기준 브랜치
        <input
          required
          value={base}
          onChange={(e) => setBase(e.target.value)}
        />
      </label>
      {parsed?.tests && (
        <>
          <ValidationEditor
            key={`${path}\0${base}`}
            path={path}
            base={base}
            value={parsed.tests}
            onChange={(tests) => changePolicy({ tests })}
          />
          <label>
            자동 수정 최대 횟수
            <input
              type="number"
              min={0}
              max={20}
              value={parsed.max_fix_attempts ?? 3}
              onChange={(e) =>
                changePolicy({ max_fix_attempts: Number(e.target.value) })
              }
            />
          </label>
          <fieldset>
            <legend>리뷰 차단 심각도</legend>
            {(["critical", "high", "medium", "low"] as const).map(
              (severity) => (
                <label className="check-row" key={severity}>
                  <input
                    type="checkbox"
                    disabled={severity === "critical" || severity === "high"}
                    checked={
                      parsed!.blocking_severities?.includes(severity) ?? false
                    }
                    onChange={(e) =>
                      changePolicy({
                        blocking_severities: e.target.checked
                          ? [...(parsed!.blocking_severities ?? []), severity]
                          : parsed!.blocking_severities!.filter(
                              (s) => s !== severity,
                            ),
                      })
                    }
                  />
                  {severity}
                </label>
              ),
            )}
          </fieldset>
        </>
      )}
      <details>
        <summary>고급 설정 · 정책 JSON 직접 편집</summary>
        <label>
          테스트·리뷰 정책 (JSON)
          <textarea
            className="mono"
            rows={16}
            required
            value={policy}
            spellCheck={false}
            onChange={(e) => setPolicy(e.target.value)}
          />
        </label>
      </details>
      <Modal
        trigger={
          <Button type="button" variant="secondary">
            정책 JSON 작성 가이드
          </Button>
        }
        open={guideOpen}
        onOpenChange={setGuideOpen}
        title="프로젝트 정책 JSON 작성 가이드"
        description="자동 감지 또는 도구 선택으로 Python·Node·Java 검증을 구성할 수 있습니다. JSON은 고급 설정에서 편집합니다."
      >
        <div className="input-guide">
          <dl>
            <dt>tests.commands</dt>
            <dd>
              테스트 명령을 하나 이상 등록합니다. 명령 ID는
              영문·숫자·밑줄·하이픈을 사용합니다. argv는 실행 파일과 인자를 나눈
              배열이며 셸 문법은 사용할 수 없습니다. 실행 파일은 대상 프로젝트
              환경에 맞게 지정하세요.
            </dd>
            <dt>timeout_seconds · junit_report</dt>
            <dd>
              제한 시간은 초 단위 양수입니다. 테스트 명령은 매번 새 JUnit XML을
              생성해야 하며 junit_report에 같은 상대 경로를 지정합니다. 테스트
              0건이나 전체 건너뛰기는 통과로 처리하지 않습니다. kind=check인
              빌드·타입 검사는 종료 코드로 판정하며 테스트를 대신하지 않습니다.
            </dd>
            <dt>tests.write_paths</dt>
            <dd>
              테스트 파일을 추가·수정할 수 있는 상대 경로입니다. 기존 테스트는
              요구사항에 따른 변경 근거를 검증한 뒤 자동 수정하며 독립 리뷰에서
              회귀 검사의 삭제·약화 여부를 확인합니다.
            </dd>
            <dt>tests.output_paths</dt>
            <dd>
              테스트 중 쓰기를 허용할 보고서·빌드·캐시 상대 경로입니다. Git이
              추적하는 소스 파일이 포함된 디렉터리는 지정하지 마세요.
            </dd>
            <dt>max_fix_attempts</dt>
            <dd>
              자동 수정 최대 횟수입니다. 0~20의 정수이며 기본값은 3입니다.
            </dd>
            <dt>blocking_severities</dt>
            <dd>
              완료를 차단할 리뷰 심각도입니다. critical·high는 반드시 포함해야
              하며 medium·low를 추가할 수 있습니다.
            </dd>
          </dl>
          <h3>Python pytest 예시</h3>
          <pre>{JSON.stringify(initialPolicy, null, 2)}</pre>
          <p className="help">
            JSON은 키와 문자열에 큰따옴표를 사용하며 주석이나 마지막 쉼표를
            허용하지 않습니다. 의존성은 실행 전에 준비해야 하며 테스트 중 외부
            네트워크는 차단됩니다. 저장한 정책은 이후 생성하는 작업에
            적용됩니다.
          </p>
        </div>
      </Modal>
      <p className="help">
        저장한 검증 정책은 새로 생성하는 작업에 적용됩니다. 기존 작업의 재개
        답변으로 명령을 변경할 수 없습니다. 정책이 맞지 않으면 여기서 수정한 뒤
        새 작업을 생성하세요.
      </p>
      {error && (
        <p role="alert" className="text-red">
          {error}
        </p>
      )}
      {!formId && (
        <div className="form-action-bar project-form-actions">
          <Button type="submit" disabled={pending}>
            {pending
              ? "저장 중…"
              : project
                ? "변경 사항 저장"
                : "프로젝트 등록"}
          </Button>
        </div>
      )}
    </form>
  );
}
