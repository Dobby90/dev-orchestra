import { useQuery } from "@tanstack/react-query";
import { ShieldCheck, Terminal } from "lucide-react";
import { api } from "../api/resources";
import { useSession } from "../hooks/session";
import { ErrorNotice, Heading, Loading } from "../components/common";
import { AgentSettingsForm } from "../components/AgentSettingsForm";
export function SettingsPage() {
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
        description="로컬 세션과 실행 서비스를 확인합니다."
      />
      <AgentSettingsForm />
      <div className="detail-grid">
        <section className="panel panel-body">
          <h2>
            <ShieldCheck size={18} />
            로컬 연결
          </h2>
          {health.isPending ? (
            <Loading />
          ) : health.error ? (
            <ErrorNotice error={health.error} retry={() => health.refetch()} />
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
            이 화면은 임의 명령 실행이나 비밀값 편집 기능을 제공하지 않습니다.
          </p>
        </section>
      </div>
    </>
  );
}
