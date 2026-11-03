import { useState, type FormEvent } from "react";
import type { Project, Schema } from "../api/types";
import { Button } from "./ui/button";
import { Modal } from "./ui/dialog";
export const initialPolicy = {
  tests: {
    commands: {
      unit: {
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
export function ProjectForm({
  project,
  pending,
  onSubmit,
}: {
  project?: Project;
  pending: boolean;
  onSubmit: (body: Schema["ProjectCreate"]) => void;
}) {
  const [guideOpen, setGuideOpen] = useState(false);
  const [name, setName] = useState(project?.name ?? ""),
    [path, setPath] = useState(project?.repository_path ?? ""),
    [base, setBase] = useState(project?.default_base_branch ?? "main"),
    [policy, setPolicy] = useState(
      JSON.stringify(project?.policy ?? initialPolicy, null, 2),
    ),
    [error, setError] = useState("");
  function submit(event: FormEvent) {
    event.preventDefault();
    try {
      const value = JSON.parse(policy);
      if (
        !value ||
        typeof value !== "object" ||
        !value.tests ||
        !Array.isArray(value.blocking_severities)
      )
        throw new Error();
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
    <form onSubmit={submit} className="form-grid">
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
          placeholder="/Users/me/projects/my-app"
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
      <Modal
        trigger={
          <Button type="button" variant="secondary">
            정책 JSON 작성 가이드
          </Button>
        }
        open={guideOpen}
        onOpenChange={setGuideOpen}
        title="프로젝트 정책 JSON 작성 가이드"
        description="기본 예시는 Python pytest용입니다. 대상 프로젝트의 테스트 도구와 환경에 맞게 수정하세요."
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
              0건이나 전체 건너뛰기는 통과로 처리하지 않습니다.
            </dd>
            <dt>tests.write_paths</dt>
            <dd>
              새 테스트 파일을 추가할 수 있는 상대 경로입니다. 기존 테스트
              수정은 허용하지 않습니다.
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
        테스트 실행 파일은 대상 프로젝트 환경에 맞게 설정하세요. argv는 인자
        배열이며 JUnit 출력 경로가 필요합니다. 변경한 정책은 이후 생성하는
        작업에 적용됩니다.
      </p>
      {error && (
        <p role="alert" className="text-red">
          {error}
        </p>
      )}
      <Button type="submit" disabled={pending}>
        {pending ? "저장 중…" : project ? "변경 사항 저장" : "프로젝트 등록"}
      </Button>
    </form>
  );
}
