import { useState } from "react";
import { api } from "../api/resources";
import type { Schema } from "../api/types";
import { ErrorNotice } from "./common";
import { Button } from "./ui/button";

type Tests = Schema["Tests"];
type Candidate = Schema["ValidationCandidate"];
const lines = (value: string) => value.split("\n").filter((v) => v.trim());

export function ValidationEditor({
  path,
  base,
  value,
  onChange,
}: {
  path: string;
  base: string;
  value: Tests;
  onChange: (value: Tests) => void;
}) {
  const [detection, setDetection] = useState<
    Schema["ValidationDetection"] | null
  >(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [profiles, setProfiles] = useState<Candidate[]>([]);
  const [busy, setBusy] = useState(false);
  const [notices, setNotices] = useState<string[]>([]);
  const [error, setError] = useState<Error | null>(null);
  async function detect() {
    setBusy(true);
    setError(null);
    setDetection(null);
    try {
      const result = await api.detectValidation({
        repository_path: path,
        base_branch: base,
      });
      setDetection(result);
      setSelected(result.candidates.map((c) => c.id));
    } catch (e) {
      setError(e as Error);
    } finally {
      setBusy(false);
    }
  }
  async function manual() {
    setError(null);
    setBusy(true);
    try {
      setProfiles(await api.validationProfiles());
    } catch (e) {
      setError(e as Error);
    } finally {
      setBusy(false);
    }
  }
  function apply(candidates: Candidate[], replace = false) {
    setNotices([...new Set(candidates.flatMap((c) => c.warnings ?? []))]);
    if (replace) setDetection(null);
    const commands = replace ? {} : { ...value.commands };
    for (const c of candidates) {
      let key = c.id;
      for (let n = 2; key in commands; n++) key = `${c.id}_${n}`;
      commands[key] = structuredClone(c.command);
    }
    onChange({
      commands,
      write_paths: [
        ...new Set([
          ...(replace ? [] : (value.write_paths ?? [])),
          ...candidates.flatMap((c) => c.write_paths),
        ]),
      ],
      output_paths: [
        ...new Set([
          ...(replace ? [] : (value.output_paths ?? [])),
          ...candidates.flatMap((c) => c.output_paths),
        ]),
      ],
    });
  }
  function update(id: string, patch: Partial<Schema["TestCommand"]>) {
    const current = value.commands?.[id];
    if (current)
      onChange({
        ...value,
        commands: { ...value.commands, [id]: { ...current, ...patch } },
      });
  }
  return (
    <fieldset className="validation-editor">
      <legend>프로젝트 검증</legend>
      <p className="help">
        기준 브랜치의 설정을 읽어 추천합니다. 감지 과정에서는 프로젝트 명령을
        실행하지 않습니다.
      </p>
      <div className="inline-actions">
        <Button
          type="button"
          variant="secondary"
          disabled={!path || !base || busy}
          onClick={detect}
        >
          {busy ? "확인 중…" : detection ? "다시 감지" : "검증 자동 감지"}
        </Button>
        <Button
          type="button"
          variant="secondary"
          disabled={busy}
          onClick={manual}
        >
          도구 직접 선택
        </Button>
      </div>
      {error && <ErrorNotice error={error} />}
      {notices.map((notice, index) => (
        <p className="notice" key={index}>
          {notice}
        </p>
      ))}
      {profiles.length > 0 && (
        <label>
          추가할 검증 도구
          <select
            value=""
            onChange={(e) => {
              const profile = profiles.find((p) => p.id === e.target.value);
              if (profile) apply([profile]);
            }}
          >
            <option value="">도구를 선택하세요</option>
            {profiles.map((p) => (
              <option key={p.id} value={p.id}>
                {p.label}
              </option>
            ))}
          </select>
          <span className="help">
            직접 선택한 예시 명령과 보고서 경로는 프로젝트에 맞게 확인하세요.
          </span>
        </label>
      )}
      {detection && (
        <section className="notice validation-candidates">
          <h3>감지된 검증 후보</h3>
          <p className="help mono path">기준 커밋 {detection.base_sha}</p>
          {detection.warnings.map((w, i) => (
            <p key={i}>{w}</p>
          ))}
          {detection.candidates.map((c) => (
            <div key={c.id}>
              <label className="check-row">
                <input
                  type="checkbox"
                  checked={selected.includes(c.id)}
                  onChange={(e) =>
                    setSelected(
                      e.target.checked
                        ? [...selected, c.id]
                        : selected.filter((id) => id !== c.id),
                    )
                  }
                />
                {c.label}
              </label>
              <p className="help">감지 근거: {(c.evidence ?? []).join(", ")}</p>
              <pre>{c.command.argv.join(" ")}</pre>
              {(c.warnings ?? []).map((w, i) => (
                <p className="help" key={i}>
                  {w}
                </p>
              ))}
            </div>
          ))}
          {detection.candidates.length > 0 && (
            <>
              <p className="help">
                적용하면 아래 검증 목록과 테스트·출력 경로를 선택한 후보로
                교체합니다. 저장 전까지 프로젝트 정책은 변경되지 않습니다.
              </p>
              <Button
                type="button"
                disabled={
                  !detection.candidates.some(
                    (c) =>
                      selected.includes(c.id) &&
                      (c.command.kind ?? "test") === "test",
                  )
                }
                onClick={() =>
                  apply(
                    detection.candidates.filter((c) => selected.includes(c.id)),
                    true,
                  )
                }
              >
                선택한 검증 적용
              </Button>
            </>
          )}
        </section>
      )}
      {Object.entries(value.commands ?? {}).length === 0 && (
        <p className="notice">
          검증을 자동 감지하거나 도구를 직접 선택하세요. 테스트 검증이 하나 이상
          필요합니다.
        </p>
      )}
      {Object.entries(value.commands ?? {}).map(([id, command]) => (
        <details className="command-result" key={id}>
          <summary>
            {(command.kind ?? "test") === "test" ? "테스트" : "빌드·정적 검사"}{" "}
            · {command.argv.join(" ")}
          </summary>
          <div className="validation-command-fields">
            <p className="help">명령 ID: {id}</p>
            <label>
              검증 종류 ({id})
              <select
                value={command.kind ?? "test"}
                onChange={(e) =>
                  update(id, {
                    kind: e.target.value as "test" | "check",
                    junit_report:
                      e.target.value === "test" ? `build/${id}.xml` : null,
                  })
                }
              >
                <option value="test">테스트 · JUnit 필수</option>
                <option value="check">빌드·타입·정적 검사 · 종료 코드</option>
              </select>
            </label>
            <label>
              실행 파일 ({id})
              <input
                required
                value={command.argv[0] ?? ""}
                onChange={(e) =>
                  update(id, {
                    argv: [e.target.value, ...command.argv.slice(1)],
                  })
                }
              />
            </label>
            <label>
              인자 · 한 줄에 하나 ({id})
              <textarea
                rows={4}
                className="mono"
                value={command.argv.slice(1).join("\n")}
                onChange={(e) =>
                  update(id, {
                    argv: [command.argv[0], ...e.target.value.split("\n")],
                  })
                }
              />
            </label>
            <label>
              작업 경로 ({id})
              <input
                required
                value={command.working_directory ?? "."}
                onChange={(e) =>
                  update(id, { working_directory: e.target.value })
                }
              />
            </label>
            {(command.kind ?? "test") === "test" && (
              <label>
                JUnit 경로 · 저장소 기준 ({id})
                <input
                  required
                  value={command.junit_report ?? ""}
                  onChange={(e) => update(id, { junit_report: e.target.value })}
                />
              </label>
            )}
            <label>
              제한 시간 · 초 ({id})
              <input
                required
                type="number"
                min={1}
                value={command.timeout_seconds ?? 600}
                onChange={(e) =>
                  update(id, { timeout_seconds: Number(e.target.value) })
                }
              />
            </label>
            <label>
              네트워크 ({id})
              <select
                value={command.network ?? "none"}
                onChange={(e) =>
                  update(id, { network: e.target.value as "none" | "loopback" })
                }
              >
                <option value="none">차단</option>
                <option value="loopback">127.0.0.1 허용 · macOS</option>
              </select>
            </label>
            {Object.entries(command.env ?? {}).map(([key, entry]) => (
              <label key={key}>
                {key} ({id})
                <input
                  value={entry}
                  onChange={(e) =>
                    update(id, {
                      env: { ...command.env, [key]: e.target.value },
                    })
                  }
                />
              </label>
            ))}
            <Button
              type="button"
              variant="ghost"
              onClick={() => {
                const commands = { ...value.commands };
                delete commands[id];
                onChange({ ...value, commands });
              }}
            >
              검증 제거 ({id})
            </Button>
          </div>
        </details>
      ))}
      <label>
        새 테스트 허용 경로 · 한 줄에 하나
        <textarea
          rows={3}
          value={(value.write_paths ?? []).join("\n")}
          onChange={(e) =>
            onChange({ ...value, write_paths: lines(e.target.value) })
          }
        />
      </label>
      <label>
        출력·캐시 허용 경로 · 한 줄에 하나
        <textarea
          rows={3}
          value={(value.output_paths ?? []).join("\n")}
          onChange={(e) =>
            onChange({ ...value, output_paths: lines(e.target.value) })
          }
        />
      </label>
      <p className="help">
        JUnit 경로는 단일 파일 또는 TEST-*.xml 패턴을 지원합니다. 테스트
        0건·전체 건너뛰기는 실패입니다. 빌드 검사는 테스트를 대신하지 않습니다.
        의존성은 실행 전에 준비해야 합니다.
      </p>
    </fieldset>
  );
}
