import { useState } from "react";
import * as Tabs from "@radix-ui/react-tabs";
import { useQuery } from "@tanstack/react-query";
import { ShieldCheck, Terminal } from "lucide-react";
import { api } from "../api/resources";
import { useSession } from "../hooks/session";
import { ErrorNotice, Heading, Loading } from "../components/common";
import { AgentSettingsForm } from "../components/AgentSettingsForm";
import { WebhookSettingsForm } from "../components/WebhookSettingsForm";
export function SettingsPage() {
  const [tab, setTab] = useState("environment");
  const session = useSession();
  const health = useQuery({
    queryKey: ["health"],
    queryFn: api.health,
    refetchInterval: 10000,
  });
  return (
    <>
      <Heading
        eyebrow="WORKSPACE / PREFERENCES"
        title="연결 및 실행 환경"
        description="서비스 상태를 확인하고 에이전트 모델과 웹훅 알림을 설정합니다."
      />
      <Tabs.Root value={tab} onValueChange={setTab}>
        <Tabs.List className="tabs-list" aria-label="연결 및 실행 환경 분류">
          <Tabs.Trigger className="tab" value="environment">
            서비스 상태
          </Tabs.Trigger>
          <Tabs.Trigger className="tab" value="agents">
            에이전트 모델
          </Tabs.Trigger>
          <Tabs.Trigger className="tab" value="notifications">
            웹훅 알림
          </Tabs.Trigger>
        </Tabs.List>
        <Tabs.Content
          className="tab-content"
          value="agents"
          forceMount
          hidden={tab !== "agents"}
        >
          <AgentSettingsForm />
        </Tabs.Content>
        <Tabs.Content
          className="tab-content"
          value="notifications"
          forceMount
          hidden={tab !== "notifications"}
        >
          <WebhookSettingsForm />
        </Tabs.Content>
        <Tabs.Content
          className="tab-content"
          value="environment"
          forceMount
          hidden={tab !== "environment"}
        >
          <div className="detail-grid">
            <section className="panel panel-body">
              <h2>
                <ShieldCheck size={18} />
                로컬 연결
              </h2>
              {health.isPending ? (
                <Loading />
              ) : health.error ? (
                <ErrorNotice
                  error={health.error}
                  retry={() => health.refetch()}
                />
              ) : (
                <p className="text-green">API 정상 응답</p>
              )}
              <dl>
                <dt>연결 주소</dt>
                <dd>127.0.0.1 · 같은 Origin 프록시</dd>
                <dt>세션 만료</dt>
                <dd>
                  {session
                    ? new Date(session.expires_at).toLocaleString("ko-KR")
                    : "—"}
                </dd>
                <dt>지원 범위</dt>
                <dd>macOS · 단일 사용자 · 신뢰하는 프로젝트</dd>
              </dl>
            </section>
            <section className="panel panel-body">
              <h2>
                <Terminal size={18} />
                실행 서비스 확인
              </h2>
              <p className="subtle">
                로컬 터미널에서 DevOrchestra 저장소 폴더로 이동한 뒤 다음
                명령으로 Dispatcher 상태를 확인하세요.
              </p>
              <pre className="command">
                .venv/bin/devorchestra worker status
              </pre>
              <p className="help">
                GUI를 별도 설정 파일로 실행했다면 같은 파일을 지정하세요. 설정
                파일을 지정하지 않고 실행했다면 위 명령을 그대로 사용합니다.
              </p>
              <pre className="command">
                .venv/bin/devorchestra --config /absolute/path/to/config.yaml
                worker status
              </pre>
            </section>
          </div>
        </Tabs.Content>
      </Tabs.Root>
    </>
  );
}
