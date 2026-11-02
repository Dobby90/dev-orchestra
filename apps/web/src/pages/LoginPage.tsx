import { useState, type FormEvent } from "react";
import { useMutation } from "@tanstack/react-query";
import { AudioLines, ArrowRight, ShieldCheck } from "lucide-react";
import { api } from "../api/resources";
import type { Schema } from "../api/types";
import { Button } from "../components/ui/button";
import { ErrorNotice } from "../components/common";
export function LoginPage({
  onSuccess,
}: {
  onSuccess: (value: Schema["SessionView"]) => void;
}) {
  const [code, setCode] = useState("");
  const login = useMutation({
    mutationFn: api.login,
    onSuccess: (value) => {
      setCode("");
      onSuccess(value);
    },
  });
  function submit(event: FormEvent) {
    event.preventDefault();
    if (!login.isPending) login.mutate(code.trim());
  }
  return (
    <main className="login-shell">
      <section className="login-card">
        <div className="brand-mark">
          <AudioLines size={28} />
        </div>
        <p className="eyebrow">LOCAL DEVELOPMENT WORKSPACE</p>
        <h1>
          작업에 집중하세요.
          <br />
          <span className="subtle">흐름은 DevOrchestra가.</span>
        </h1>
        <p className="subtle">
          기획부터 검증까지, 로컬 개발 워크플로를 한곳에서 확인하세요.
        </p>
        <form onSubmit={submit}>
          <label htmlFor="bootstrap">일회용 로그인 코드</label>
          <input
            id="bootstrap"
            type="password"
            autoComplete="off"
            required
            minLength={32}
            maxLength={128}
            value={code}
            onChange={(e) => setCode(e.target.value)}
          />
          <p className="help">
            같은 설정의 터미널에서{" "}
            <code>devorchestra --config config.yaml web-session</code>으로
            발급합니다. 코드는 60초 동안 유효합니다.
          </p>
          {login.error && <ErrorNotice error={login.error} />}
          <Button type="submit" disabled={login.isPending}>
            {login.isPending ? "확인 중…" : "작업 공간 열기"}
            <ArrowRight size={16} />
          </Button>
        </form>
        <div className="login-footer">
          <ShieldCheck size={15} />이 컴퓨터에서 실행되는 로컬 전용 작업 공간
        </div>
      </section>
    </main>
  );
}
