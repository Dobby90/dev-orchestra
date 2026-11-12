import { useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/resources";
import type { Schema } from "../api/types";
import { ErrorNotice, Loading, roles } from "./common";
import { Button } from "./ui/button";

const roleIds = [
  "planner",
  "developer",
  "tester",
  "reviewer",
  "fixer",
] as const;
type Options = Schema["AgentOptions"];
type Settings = Schema["AgentSettings"];

export function AgentSettingsForm() {
  const client = useQueryClient();
  const [saved, setSaved] = useState(false);
  const settings = useQuery({
    queryKey: ["agent-settings"],
    queryFn: api.agentSettings,
    staleTime: Infinity,
  });
  return (
    <section className="panel panel-body agent-settings">
      <h2>에이전트 모델 설정</h2>
      <p className="subtle">
        저장한 설정은 새 작업에 적용됩니다. 진행 중인 작업과 과거 작업은 생성
        당시 설정을 유지합니다.
      </p>
      {saved && (
        <p role="status" className="text-green">
          에이전트 설정을 저장했습니다.
        </p>
      )}
      {settings.isPending ? (
        <Loading />
      ) : settings.error ? (
        <ErrorNotice error={settings.error} retry={() => settings.refetch()} />
      ) : (
        <SettingsEditor
          key={settings.data.version}
          initial={settings.data}
          onSaved={(value) => {
            setSaved(true);
            client.setQueryData(["agent-settings"], value);
          }}
          onEdited={() => setSaved(false)}
          reload={() => settings.refetch()}
        />
      )}
    </section>
  );
}

function SettingsEditor({
  initial,
  onSaved,
  onEdited,
  reload,
}: {
  initial: Schema["AgentSettingsView"];
  onSaved: (value: Schema["AgentSettingsView"]) => void;
  onEdited: () => void;
  reload: () => void;
}) {
  const [value, setValue] = useState<Settings>(initial.settings);
  const catalog = useMutation({ mutationFn: api.codexModels });
  const save = useMutation({
    mutationFn: api.saveAgentSettings,
    onSuccess: onSaved,
  });
  const models = catalog.data?.items ?? [];
  const changed = JSON.stringify(value) !== JSON.stringify(initial.settings);
  function update(role: (typeof roleIds)[number] | null, options: Options) {
    onEdited();
    save.reset();
    setValue((previous) =>
      role
        ? { ...previous, roles: { ...previous.roles, [role]: options } }
        : { ...previous, ...options },
    );
  }
  function issue(options: Options) {
    const model = models.find((m) => m.model === options.model);
    if (options.model && !model)
      return "모델 목록에서 사용할 모델을 선택해 주세요.";
    if (
      options.reasoning_effort &&
      (!model || !model.reasoning_efforts.includes(options.reasoning_effort))
    )
      return "선택한 모델에서 지원하는 추론 강도를 지정해 주세요.";
    return "";
  }
  const invalid = catalog.data
    ? [
        issue(value),
        ...roleIds.map((role) => {
          const override = value.roles?.[role];
          return issue({
            model: override?.model ?? value.model,
            reasoning_effort:
              override?.reasoning_effort ?? value.reasoning_effort,
          });
        }),
      ].find(Boolean)
    : "";
  function submit(event: FormEvent) {
    event.preventDefault();
    if (invalid || !changed) return;
    save.mutate({ settings: value, expected_version: initial.version });
  }
  function row(role: (typeof roleIds)[number] | null) {
    const options = role ? (value.roles?.[role] ?? {}) : value;
    const label = role ? roles[role] : "공통 기본값";
    const effectiveModel = options.model ?? (role ? value.model : null);
    const model = models.find((m) => m.model === effectiveModel);
    const defaultLabel = role ? "공통 기본값 상속" : "Codex 기본값 사용";
    return (
      <fieldset
        key={role ?? "default"}
        className={`agent-options ${role ? "agent-role-row" : "agent-default-row"}`}
        disabled={save.isPending}
      >
        <legend>{label}</legend>
        <label>
          {label} 모델
          <select
            value={options.model ?? ""}
            disabled={!catalog.data || catalog.isPending}
            onChange={(e) =>
              update(role, {
                ...options,
                model: e.target.value || null,
                reasoning_effort: null,
              })
            }
          >
            <option value="">{defaultLabel}</option>
            {options.model &&
              !models.some((m) => m.model === options.model) && (
                <option value={options.model}>
                  {options.model} · 기존 설정
                </option>
              )}
            {models.map((m) => (
              <option key={m.model} value={m.model}>
                {m.model}
              </option>
            ))}
          </select>
        </label>
        <label>
          {label} 추론 강도
          <select
            value={options.reasoning_effort ?? ""}
            disabled={!catalog.data || catalog.isPending || !model}
            onChange={(e) =>
              update(role, {
                ...options,
                reasoning_effort: e.target.value || null,
              })
            }
          >
            <option value="">{defaultLabel}</option>
            {options.reasoning_effort &&
              !model?.reasoning_efforts.includes(options.reasoning_effort) && (
                <option value={options.reasoning_effort}>
                  {options.reasoning_effort} · 기존 설정
                </option>
              )}
            {model?.reasoning_efforts.map((effort) => (
              <option key={effort} value={effort}>
                {effort}
              </option>
            ))}
          </select>
        </label>
        {role && (
          <p className="help">
            적용: {effectiveModel ?? "Codex 기본값"} · 추론{" "}
            {options.reasoning_effort ??
              value.reasoning_effort ??
              "Codex 기본값"}
          </p>
        )}
      </fieldset>
    );
  }
  return (
    <form onSubmit={submit} className="form-grid">
      <div className="inline-actions">
        <Button
          type="button"
          variant="secondary"
          disabled={catalog.isPending || save.isPending}
          onClick={() => catalog.mutate()}
        >
          {catalog.isPending ? "모델 목록 확인 중…" : "모델 목록 불러오기"}
        </Button>
        <Button
          type="button"
          variant="ghost"
          disabled={save.isPending}
          onClick={reload}
        >
          저장된 설정 다시 불러오기
        </Button>
      </div>
      <p className="help">
        모델 목록을 불러오면 Codex가 제공하는 모델과 지원 추론 강도를 선택할 수
        있습니다. 이 조회는 모델 작업을 실행하지 않습니다.
      </p>
      {catalog.error && <ErrorNotice error={catalog.error} />}
      {row(null)}
      <div className="agent-role-matrix" aria-label="역할별 모델과 추론 강도">
        {roleIds.map(row)}
      </div>
      {invalid && (
        <p role="alert" className="text-amber">
          {invalid}
        </p>
      )}
      {save.error && <ErrorNotice error={save.error} />}
      <Button
        type="submit"
        disabled={
          !changed ||
          !!invalid ||
          !catalog.data ||
          catalog.isPending ||
          save.isPending
        }
      >
        {save.isPending ? "저장 중…" : "에이전트 설정 저장"}
      </Button>
    </form>
  );
}
