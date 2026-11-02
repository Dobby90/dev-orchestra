import { useState, type FormEvent } from "react";
import type { Project, Schema } from "../api/types";
import { Button } from "./ui/button";
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
