import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/resources";
import type { Schema } from "../api/types";
import { ErrorNotice, Loading } from "./common";
import { Button } from "./ui/button";

export function WebhookSettingsForm() {
  const [saved, setSaved] = useState(false);
  const query = useQuery({
    queryKey: ["webhook-settings"],
    queryFn: api.webhookSettings,
    refetchInterval: 5000,
  });
  return (
    <section className="panel panel-body" aria-label="웹훅 알림 설정">
      <h2>웹훅 알림</h2>
      <p className="subtle">
        입력 필요·일시중지·기획 승인 대기·최종 승인 대기·실패 알림을 선택한
        앱에도 발송합니다. 브라우저를 닫아도 API 또는 Dispatcher가 실행 중이면
        발송됩니다.
      </p>
      {saved && <p role="status">웹훅 설정을 저장했습니다.</p>}
      {query.isPending ? (
        <Loading />
      ) : query.error ? (
        <ErrorNotice error={query.error} retry={() => query.refetch()} />
      ) : (
        <WebhookForm
          key={query.data.version}
          value={query.data}
          onSaved={() => setSaved(true)}
          onEdited={() => setSaved(false)}
        />
      )}
    </section>
  );
}

function WebhookForm({
  value,
  onSaved,
  onEdited,
}: {
  value: Schema["WebhookSettingsView"];
  onSaved: () => void;
  onEdited: () => void;
}) {
  const client = useQueryClient();
  const [enabled, setEnabled] = useState(value.enabled);
  const [app, setApp] = useState(value.app);
  const [url, setUrl] = useState("");
  const changed = enabled !== value.enabled || app !== value.app || !!url;
  const update = (data: Schema["WebhookSettingsView"]) =>
    client.setQueryData(["webhook-settings"], data);
  const save = useMutation({
    mutationFn: () => {
      const body = {
        expected_version: value.version,
        enabled,
        app,
        url: url || null,
      };
      setUrl("");
      return api.saveWebhookSettings(body);
    },
    onSuccess: (data) => {
      update(data);
      onSaved();
    },
  });
  const test = useMutation({
    mutationFn: () => api.testWebhook(value.version),
    onSuccess: update,
  });
  const pending = save.isPending || test.isPending;
  return (
    <form
      className="form-grid"
      onSubmit={(e) => {
        e.preventDefault();
        save.mutate();
      }}
      onChange={onEdited}
    >
      <label className="check-row">
        <input
          type="checkbox"
          checked={enabled}
          disabled={pending}
          onChange={(e) => setEnabled(e.target.checked)}
        />
        웹훅 알림 사용
      </label>
      <label>
        알림 앱
        <select
          value={app}
          disabled={pending}
          onChange={(e) => setApp(e.target.value)}
        >
          {value.apps.map((item) => (
            <option key={item.id} value={item.id}>
              {item.label}
            </option>
          ))}
        </select>
      </label>
      <label>
        웹훅 URL
        <input
          type="password"
          autoComplete="new-password"
          maxLength={2048}
          value={url}
          disabled={pending}
          placeholder={
            value.url_configured
              ? "등록됨 · 변경할 때만 새 URL 입력"
              : "https://mattermost.example/hooks/WEBHOOK_ID"
          }
          onChange={(e) => setUrl(e.target.value)}
        />
      </label>
      <p className="help">
        공인 HTTPS 주소를 사용합니다. URL은 저장 후 표시하지 않으며, 빈칸으로
        저장하면 기존 URL을 유지합니다. 설정을 저장하면 대기 중인 발송은
        초기화하고 저장 시점 이후의 새 알림만 발송합니다.
      </p>
      <div className="inline-actions">
        <Button
          type="submit"
          disabled={
            pending || !changed || (enabled && !url && !value.url_configured)
          }
        >
          {save.isPending ? "저장 중…" : "웹훅 설정 저장"}
        </Button>
        <Button
          type="button"
          variant="secondary"
          disabled={pending || changed || !value.url_configured}
          onClick={() => test.mutate()}
        >
          {test.isPending ? "발송 중…" : "테스트 발송"}
        </Button>
      </div>
      {save.error && <ErrorNotice error={save.error} />}
      {test.error && <ErrorNotice error={test.error} />}
      {value.last_delivery && (
        <p role="status">
          {value.last_delivery.message}{" "}
          {new Date(value.last_delivery.occurred_at).toLocaleString("ko-KR")}
          {value.last_delivery.event_id !== null &&
            ` · 알림 ${value.last_delivery.event_id} · 시도 ${value.last_delivery.attempts}/3`}
        </p>
      )}
      <p className="help">
        연결 테스트는 저장한 앱에 실제 메시지 1건을 보냅니다. 자동 발송은 연결
        실패 시 최대 3회 시도하며, 응답 유실 시 같은 알림 ID가 중복 수신될 수
        있습니다. 웹훅 실패는 작업 실행이나 화면 알림을 변경하지 않습니다.
      </p>
    </form>
  );
}
