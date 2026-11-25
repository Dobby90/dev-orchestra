import { useState } from "react";
import { api } from "../api/resources";
import type { Schema } from "../api/types";
import { ErrorNotice } from "./common";
import { Button } from "./ui/button";

type Tests = Schema["Tests"];
type Candidate = Schema["ValidationCandidate"];
const lines = (value: string) => value.split("\n").filter((v) => v.trim());

export function ValidationEditor({
  value,
  onChange,
}: {
  value: Tests;
  onChange: (value: Tests) => void;
}) {
  const [profiles, setProfiles] = useState<Candidate[]>([]);
  const [busy, setBusy] = useState(false);
  const [notices, setNotices] = useState<string[]>([]);
  const [error, setError] = useState<Error | null>(null);
  const [requiredDraft, setRequiredDraft] = useState<string | null>(null);
  const required = value.required_command_ids ?? [];
  const missing = required.filter(
    (id) => !Object.hasOwn(value.commands ?? {}, id),
  );
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
  function apply(candidates: Candidate[]) {
    setNotices([...new Set(candidates.flatMap((c) => c.warnings ?? []))]);
    const commands = { ...value.commands };
    for (const c of candidates) {
      let key = c.id;
      for (let n = 2; key in commands; n++) key = `${c.id}_${n}`;
      commands[key] = structuredClone(c.command);
    }
    onChange({
      ...value,
      commands,
      write_paths: [
        ...new Set([
          ...(value.write_paths ?? []),
          ...candidates.flatMap((c) => c.write_paths),
        ]),
      ],
      output_paths: [
        ...new Set([
          ...(value.output_paths ?? []),
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
        기존 테스트는 새 작업에서 자동으로 재사용합니다. 실행 명령을 직접
        지정해야 하는 경우에만 아래 설정을 변경하세요.
      </p>
      <label>
        필수 검증 명령 ID · 한 줄에 하나
        <textarea
          rows={2}
          value={requiredDraft ?? required.join("\n")}
          onBlur={() => setRequiredDraft(null)}
          onChange={(e) => {
            setRequiredDraft(e.target.value);
            onChange({
              ...value,
              required_command_ids: lines(e.target.value).map((id) =>
                id.trim(),
              ),
            });
          }}
        />
        <span className="help">
          예를 들어 테스트와 lint를 요구하면 두 명령의 ID를 등록하세요. 필수
          명령이 빠지면 실행 전 확인에서 새 작업 생성을 차단합니다.
        </span>
      </label>
      {missing.length > 0 && (
        <p className="notice">등록이 필요한 명령 {missing.join(", ")}</p>
      )}
      <div className="inline-actions">
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
      {Object.entries(value.commands ?? {}).length === 0 && (
        <p className="notice">
          비워 두면 새 작업에서 검증을 자동 설정합니다. 직접 지정하려면 도구를
          선택하세요.
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
