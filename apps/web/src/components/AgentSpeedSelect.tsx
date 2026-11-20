import type { Schema } from "../api/types";

type ServiceTier = Schema["AgentOptions"]["service_tier"];

export function agentSpeedLabel(tier: ServiceTier) {
  if (tier === "fast") return "패스트";
  if (tier === "default") return "일반";
  return "Codex 기본값";
}

export function AgentSpeedSelect({
  label,
  value,
  inherit,
  disabled,
  supportsFast,
  onChange,
}: {
  label: string;
  value: ServiceTier;
  inherit: boolean;
  disabled: boolean;
  supportsFast: boolean;
  onChange: (tier: ServiceTier) => void;
}) {
  return (
    <label>
      {label} 속도
      <select
        value={value ?? ""}
        disabled={disabled}
        onChange={(e) => {
          const tier = e.target.value;
          onChange(tier === "fast" || tier === "default" ? tier : null);
        }}
      >
        <option value="">
          {inherit ? "공통 기본값 상속" : "Codex 기본값 사용"}
        </option>
        <option value="default">{agentSpeedLabel("default")}</option>
        <option value="fast" disabled={!supportsFast}>
          {supportsFast ? agentSpeedLabel("fast") : "패스트 (지원 모델 필요)"}
        </option>
      </select>
    </label>
  );
}
