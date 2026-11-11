import { useMutation } from "@tanstack/react-query";
import { api } from "../api/resources";
import type { Schema } from "../api/types";
import { ErrorNotice, roles } from "./common";
import { Button } from "./ui/button";

type Settings = Schema["AgentSettings"];
const roleIds = [
  "planner",
  "developer",
  "tester",
  "reviewer",
  "fixer",
] as const;

export function RunAgentSettings({
  value,
  onChange,
}: {
  value: Settings | null;
  onChange: (value: Settings | null) => void;
}) {
  const defaults = useMutation({
    mutationFn: api.agentSettings,
    onSuccess: (data) => onChange(data.settings),
  });
  const catalog = useMutation({ mutationFn: api.codexModels });
  const models = catalog.data?.items ?? [];
  return (
    <fieldset>
      <legend>에이전트 모델·추론 강도</legend>
      <p className="help">
        기본 설정을 사용하거나 이번 작업에만 적용할 설정을 지정합니다. 생성
        후에는 저장된 설정을 유지합니다.
      </p>
      <Button
        type="button"
        variant="secondary"
        disabled={defaults.isPending}
        onClick={() => (value ? onChange(null) : defaults.mutate())}
      >
        {value ? "기본 설정 사용" : "이 작업만 변경"}
      </Button>
      {defaults.error && <ErrorNotice error={defaults.error} />}
      {value && (
        <>
          <Button
            type="button"
            variant="secondary"
            disabled={catalog.isPending}
            onClick={() => catalog.mutate()}
          >
            모델 목록 불러오기
          </Button>
          {catalog.error && <ErrorNotice error={catalog.error} />}
          {[null, ...roleIds].map((role) => {
            const options = role ? (value.roles?.[role] ?? {}) : value;
            const label = role ? roles[role] : "공통 기본값";
            const effectiveModel = options.model ?? (role ? value.model : null);
            const model = models.find((m) => m.model === effectiveModel);
            const update = (options: Schema["AgentOptions"]) =>
              onChange(
                role
                  ? { ...value, roles: { ...value.roles, [role]: options } }
                  : { ...value, ...options },
              );
            return (
              <fieldset key={role ?? "default"}>
                <legend>{label}</legend>
                <label>
                  {label} 모델
                  <select
                    value={options.model ?? ""}
                    disabled={!catalog.data || catalog.isPending}
                    onChange={(e) =>
                      update({
                        ...options,
                        model: e.target.value || null,
                        reasoning_effort: null,
                      })
                    }
                  >
                    <option value="">
                      {role ? "공통 기본값 상속" : "Codex 기본값 사용"}
                    </option>
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
                      update({
                        ...options,
                        reasoning_effort: e.target.value || null,
                      })
                    }
                  >
                    <option value="">
                      {role ? "공통 기본값 상속" : "Codex 기본값 사용"}
                    </option>
                    {options.reasoning_effort &&
                      !model?.reasoning_efforts.includes(
                        options.reasoning_effort,
                      ) && (
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
                <p className="help">
                  적용: {effectiveModel ?? "Codex 기본값"} · 추론{" "}
                  {options.reasoning_effort ??
                    (role ? value.reasoning_effort : null) ??
                    "Codex 기본값"}
                </p>
              </fieldset>
            );
          })}
        </>
      )}
    </fieldset>
  );
}
