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
  const [tab, setTab] = useState("agents");
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
        description="에이전트 모델, 웹훅 알림, 로컬 실행 환경을 분류별로 설정합니다."
      />
      <Tabs.Root value={tab} onValueChange={setTab}>
        <Tabs.List className="tabs-list" aria-label="연결 및 실행 환경 분류">
          <Tabs.Trigger className="tab" value="agents">
            에이전트 모델
          </Tabs.Trigger>
          <Tabs.Trigger className="tab" value="notifications">
            웹훅 알림
          </Tabs.Trigger>
          <Tabs.Trigger className="tab" value="environment">
            연결·실행 환경
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
                API와 독립 Dispatcher는 같은 설정 파일을 사용해야 합니다. 실행
                상태는 터미널에서 확인할 수 있습니다.
              </p>
              <pre className="command">
                devorchestra --config config.yaml worker status
              </pre>
              <p className="help">
                이 화면은 임의 명령 실행이나 비밀값 편집 기능을 제공하지
                않습니다.
              </p>
            </section>
          </div>
        </Tabs.Content>
      </Tabs.Root>
    </>
  );
}
