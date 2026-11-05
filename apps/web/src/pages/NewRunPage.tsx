import { useState, type FormEvent } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { useInfiniteQuery, useMutation, useQuery } from "@tanstack/react-query";
import {
  ArrowLeft,
  ArrowRight,
  Check,
  FileText,
  GitBranch,
  ShieldCheck,
} from "lucide-react";
import { api } from "../api/resources";
import { ApiError } from "../api/client";
import type { Schema } from "../api/types";
import { Heading, ErrorNotice } from "../components/common";
import { Button } from "../components/ui/button";
import { PrdGuide } from "../components/PrdGuide";
export function NewRunPage() {
  const [params] = useSearchParams(),
    navigate = useNavigate();
  const [project, setProject] = useState(params.get("project") ?? ""),
    [text, setText] = useState(""),
    [mode, setMode] = useState<"text" | "file">("text"),
    [base, setBase] = useState(""),
    [branch, setBranch] = useState(""),
    [instructions, setInstructions] = useState(""),
    [budget, setBudget] = useState(""),
    [testScope, setTestScope] = useState<"full" | "affected">("full"),
    [localError, setLocalError] = useState("");
  const projects = useInfiniteQuery({
    queryKey: ["projects", "choices"],
    queryFn: ({ pageParam }) => api.projects(pageParam),
    initialPageParam: "",
    getNextPageParam: (page) => (page.has_more ? page.next_cursor : undefined),
  });
  const selected = useQuery({
    queryKey: ["project", project],
    queryFn: () => api.project(project),
    enabled: !!project,
  });
  const branches = useQuery({
    queryKey: ["branches", project],
    queryFn: () => api.branches(project),
    enabled: !!project,
  });
  const upload = useMutation({ mutationFn: api.upload });
  const preview = useMutation({
    mutationFn: async (proposal: Schema["Proposal"]) => ({
      proposal,
      receipt: await api.preview(proposal),
      key: crypto.randomUUID(),
    }),
  });
  const submit = useMutation({
    mutationFn: () =>
      api.submit(
        {
          ...preview.data!.proposal,
          preview_token: preview.data!.receipt.preview_token,
        },
        preview.data!.key,
      ),
    onSuccess: (value) => navigate(`/runs/${value.run_id}`),
  });
  const uncertain =
    submit.error instanceof ApiError &&
    (submit.error.status === 0 || submit.error.status >= 500);
  const locked = submit.isPending || uncertain;
  function edit(action: () => void) {
    if (locked) return;
    preview.reset();
    submit.reset();
    setLocalError("");
    action();
  }
  function check(event: FormEvent) {
    event.preventDefault();
    if (!project) return;
    setLocalError("");
    if (
      mode === "text" &&
      (!text.trim() || new TextEncoder().encode(text).length > 1048576)
    ) {
      setLocalError("PRD를 1 MiB 이내의 텍스트로 입력해 주세요.");
      return;
    }
    if (mode === "file" && !upload.data) {
      setLocalError("PRD 파일을 먼저 업로드해 주세요.");
      return;
    }
    const proposal: Schema["Proposal"] = {
      project_id: project,
      spec:
        mode === "text"
          ? { kind: "text", text }
          : { kind: "stored", spec_id: upload.data!.id },
      additional_instructions: instructions,
      test_scope: testScope,
      run_tests: true,
      run_review: true,
      ...(base ? { base_branch: base } : {}),
      ...(branch ? { feature_branch: branch } : {}),
      ...(budget !== "" ? { max_fix_attempts: Number(budget) } : {}),
    };
    preview.mutate(proposal);
  }
  const choices = projects.data?.pages.flatMap((p) => p.items) ?? [];
  return (
    <>
      <Link className="back-link" to="/">
        <ArrowLeft size={14} />
        작업 목록으로
      </Link>
      <Heading
        eyebrow="WORKSPACE / NEW RUN"
        title="새 개발 작업"
        description="요구사항과 정책을 확인하고, 격리된 작업 공간에서 실행합니다."
      />
      <div className="new-run-grid">
        <section className="panel panel-body">
          <h2>
            <FileText size={18} />
            작업 요구사항
          </h2>
          <form onSubmit={check} className="form-grid">
            <fieldset disabled={locked || preview.isPending}>
              <label>
                프로젝트
                <select
                  required
                  value={project}
                  onChange={(e) =>
                    edit(() => {
                      setProject(e.target.value);
                      setBase("");
                      setBudget("");
                    })
                  }
                >
                  <option value="">프로젝트 선택</option>
                  {selected.data && !choices.some((p) => p.id === project) && (
                    <option value={project}>{selected.data.name}</option>
                  )}
                  {choices.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name}
                    </option>
                  ))}
                </select>
              </label>
              {projects.hasNextPage && (
                <Button
                  type="button"
                  variant="ghost"
                  disabled={projects.isFetchingNextPage}
                  onClick={() => projects.fetchNextPage()}
                >
                  프로젝트 더 보기
                </Button>
              )}
              {projects.error && (
                <ErrorNotice
                  error={projects.error}
                  retry={() => projects.refetch()}
                />
              )}
              <div className="segmented" aria-label="PRD 입력 방식">
                <Button
                  type="button"
                  variant={mode === "text" ? "secondary" : "ghost"}
                  aria-pressed={mode === "text"}
                  onClick={() => edit(() => setMode("text"))}
                >
                  텍스트 입력
                </Button>
                <Button
                  type="button"
                  variant={mode === "file" ? "secondary" : "ghost"}
                  aria-pressed={mode === "file"}
                  onClick={() => edit(() => setMode("file"))}
                >
                  파일 업로드
                </Button>
              </div>
              <PrdGuide />
              {mode === "text" ? (
                <label>
                  PRD
                  <textarea
                    rows={12}
                    required
                    placeholder="무엇을 만들고, 어떤 조건으로 검증할지 적어 주세요."
                    value={text}
                    onChange={(e) => edit(() => setText(e.target.value))}
                  />
                </label>
              ) : (
                <label>
                  PRD 파일 (.md / .txt, UTF-8)
                  <input
                    type="file"
                    accept=".md,.txt"
                    disabled={upload.isPending}
                    onChange={(e) => {
                      const f = e.target.files?.[0];
                      edit(() => upload.reset());
                      if (!f) return;
                      if (f.size > 1048576) {
                        setLocalError("파일은 1 MiB 이하여야 합니다.");
                        return;
                      }
                      upload.mutate(f);
                    }}
                  />
                  {upload.isPending ? (
                    <span>업로드 중…</span>
                  ) : (
                    upload.data && (
                      <span className="text-green">
                        <Check size={14} />
                        PRD 저장됨 · {upload.data.bytes.toLocaleString()} bytes
                      </span>
                    )
                  )}
                </label>
              )}
              <div className="form-columns">
                <label>
                  기준 브랜치
                  <select
                    value={base}
                    onChange={(e) => edit(() => setBase(e.target.value))}
                  >
                    <option value="">
                      기본값 ({selected.data?.default_base_branch ?? "main"})
                    </option>
                    {branches.data?.items.map((b) => (
                      <option key={b}>{b}</option>
                    ))}
                  </select>
                </label>
                <label>
                  Feature 브랜치 (선택)
                  <input
                    value={branch}
                    maxLength={200}
                    aria-describedby="feature-branch-guide"
                    placeholder="자동 생성"
                    onChange={(e) => edit(() => setBranch(e.target.value))}
                  />
                </label>
              </div>
              <p id="feature-branch-guide" className="help">
                Feature 브랜치는 비워 두면 자동 생성됩니다.
                {selected.data && (
                  <>
                    {" "}
                    직접 입력할 때는 <code>{selected.data.branch_prefix}/</code>
                    로 시작해야 합니다. 예:{" "}
                    <code>{selected.data.branch_prefix}/add-task-search</code>
                  </>
                )}
              </p>
              <label>
                추가 지시사항 (선택)
                <textarea
                  rows={3}
                  value={instructions}
                  maxLength={65536}
                  onChange={(e) => edit(() => setInstructions(e.target.value))}
                />
              </label>
              <label>
                테스트 범위
                <select
                  value={testScope}
                  onChange={(e) =>
                    edit(() =>
                      setTestScope(e.target.value as "full" | "affected"),
                    )
                  }
                >
                  <option value="full">전체 테스트</option>
                  <option value="affected">변경 영향 범위 테스트</option>
                </select>
              </label>
              <p className="help">
                생성 후에는 범위를 변경할 수 없습니다. 영향 판단 근거가
                부족하거나 공통 코드가 바뀌면 전체 검사로 확대합니다.
              </p>
              <label>
                자동 수정 최대 횟수
                <input
                  type="number"
                  min={0}
                  max={20}
                  value={budget}
                  placeholder={`프로젝트 기본값: ${selected.data?.policy.max_fix_attempts ?? 3}`}
                  onChange={(e) => edit(() => setBudget(e.target.value))}
                />
              </label>
            </fieldset>
            {localError && (
              <p role="alert" className="text-red">
                {localError}
              </p>
            )}
            {(preview.error ||
              upload.error ||
              branches.error ||
              selected.error) && (
              <ErrorNotice
                error={
                  preview.error ??
                  upload.error ??
                  branches.error ??
                  selected.error
                }
              />
            )}
            <Button
              type="submit"
              disabled={
                locked ||
                preview.isPending ||
                !project ||
                upload.isPending ||
                !!selected.data?.archived_at ||
                !!selected.error
              }
            >
              실행 전 확인
              <ArrowRight size={16} />
            </Button>
          </form>
        </section>
        <aside>
          <section className="panel panel-body policy-summary">
            <p className="eyebrow">EXECUTION POLICY</p>
            <h2>검증을 포함하는 개발 흐름</h2>
            {selected.data && (
              <section>
                <h3>실행할 검증 명령</h3>
                {Object.entries(selected.data.policy.tests.commands ?? {}).map(
                  ([id, command]) => (
                    <div key={id}>
                      <strong>
                        {id} ·{" "}
                        {(command.kind ?? "test") === "test"
                          ? "테스트"
                          : "빌드·정적 검사"}
                      </strong>
                      <pre>{command.argv.join(" ")}</pre>
                      <p className="help">
                        작업 경로: {command.working_directory ?? "."}
                        {command.junit_report
                          ? ` · JUnit: ${command.junit_report}`
                          : ""}
                      </p>
                    </div>
                  ),
                )}
                <Link to={`/projects/${selected.data.id}`}>
                  프로젝트 검증 설정 열기
                </Link>
                <p className="help">
                  기준 브랜치의 도구와 명령이 맞는지 확인하세요. 작업 생성 시
                  정책을 고정합니다.
                </p>
              </section>
            )}
            <ol className="workflow-list">
              {[
                "Planning",
                "Developer",
                "Testing",
                "Review",
                "Human approval",
              ].map((s, i) => (
                <li key={s}>
                  <span>{i + 1}</span>
                  {s}
                </li>
              ))}
            </ol>
            <p>
              <ShieldCheck size={16} />
              테스트와 코드 리뷰는 필수입니다.
            </p>
            <p className="subtle text-small">
              모델 사용량이 발생할 수 있습니다. 접수 후에는 브라우저를 닫아도
              작업이 계속됩니다.
            </p>
          </section>
          {preview.data && (
            <section className="panel panel-body preview-card">
              <p className="eyebrow">READY TO SUBMIT</p>
              <h2>
                <GitBranch size={18} />
                실행 미리보기
              </h2>
              <dl>
                <dt>프로젝트</dt>
                <dd>{selected.data?.name}</dd>
                <dt>Feature 브랜치</dt>
                <dd className="mono">{preview.data.receipt.branch}</dd>
                <dt>기준 커밋</dt>
                <dd className="mono path">{preview.data.receipt.base_sha}</dd>
                <dt>테스트 범위</dt>
                <dd>
                  {preview.data.proposal.test_scope === "affected"
                    ? "변경 영향 범위 테스트"
                    : "전체 테스트"}
                </dd>
                <dt>최대 수정 횟수</dt>
                <dd>
                  {preview.data.proposal.max_fix_attempts ??
                    selected.data?.policy.max_fix_attempts ??
                    3}
                  회
                </dd>
                <dt>유효 시각</dt>
                <dd>
                  {new Date(
                    preview.data.receipt.expires * 1000,
                  ).toLocaleTimeString("ko-KR")}
                </dd>
              </dl>
              <p className="help">{preview.data.receipt.test_scope_notice}</p>
              {(preview.data.receipt.validation_warnings ?? []).map(
                (warning, index) => (
                  <p className="notice" key={index}>
                    {warning}
                  </p>
                ),
              )}
              <p className="help">
                접수 후 작업 큐에서 실행 순서를 기다립니다.
              </p>
              {submit.error && <ErrorNotice error={submit.error} />}
              <Button
                disabled={submit.isPending}
                onClick={() => {
                  if (!submit.isPending) submit.mutate();
                }}
              >
                {submit.isPending
                  ? "접수 중…"
                  : uncertain
                    ? "동일 요청으로 접수 확인"
                    : "확인하고 작업 시작"}
                <ArrowRight size={16} />
              </Button>
              {uncertain && (
                <p role="status" className="help">
                  응답을 받지 못해 접수 여부가 확정되지 않았습니다. 같은 요청
                  키로 안전하게 확인합니다.
                </p>
              )}
            </section>
          )}
        </aside>
      </div>
    </>
  );
}
