import { useEffect, useState, type FormEvent } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { useInfiniteQuery, useMutation, useQuery } from "@tanstack/react-query";
import {
  ArrowLeft,
  ArrowRight,
  Check,
  FileText,
  ShieldCheck,
} from "lucide-react";
import { api } from "../api/resources";
import { ApiError } from "../api/client";
import type { Schema } from "../api/types";
import { Heading, ErrorNotice } from "../components/common";
import { Button } from "../components/ui/button";
import { Modal } from "../components/ui/dialog";
import { RunAgentSettings } from "../components/RunAgentSettings";
import { agentSpeedLabel } from "../components/AgentSpeedSelect";
import { useDraftField, useClearDraft } from "../hooks/runDraft";
import { PrdGuide } from "../components/PrdGuide";
import { DesignImageList } from "../components/DesignImageList";
import {
  ProjectGitConnection,
  useProjectGitConnection,
} from "../components/ProjectGitConnection";

const maxDesignImages = 8;
const maxDesignImageBytes = 5 * 1024 * 1024;

function stagedImages(images: Schema["DesignImageUpload"][]) {
  return images.map(({ id, name }) => ({
    name,
    src: api.stagedDesignImageUrl(id),
  }));
}

export function NewRunPage() {
  const [params] = useSearchParams(),
    navigate = useNavigate();
  const clearDraft = useClearDraft();
  const [agentSettings, setAgentSettings] = useDraftField("agentSettings");
  const [designImages, setDesignImages] = useDraftField("designImages");
  const [project, setProject] = useDraftField(
      "project",
      params.get("project") ?? "",
    ),
    [name, setName] = useDraftField("name"),
    [text, setText] = useDraftField("text"),
    [mode, setMode] = useDraftField("mode"),
    [base, setBase] = useDraftField("base"),
    [branch, setBranch] = useDraftField("branch"),
    [commitPrefix, setCommitPrefix] = useDraftField("commitPrefix"),
    [instructions, setInstructions] = useDraftField("instructions"),
    [budget, setBudget] = useDraftField("budget"),
    [testScope, setTestScope] = useDraftField("testScope"),
    [autoApprovePlan, setAutoApprovePlan] = useDraftField("autoApprovePlan"),
    [previewOpen, setPreviewOpen] = useState(false),
    [localError, setLocalError] = useState("");
  useEffect(() => {
    const requested = params.get("project");
    if (requested && requested !== project) {
      setProject(requested);
      setBase("");
      setBudget("");
    }
  }, [params]);
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
  const gitConnection = useProjectGitConnection(project);
  const upload = useMutation({ mutationFn: api.upload });
  const imageUpload = useMutation({
    mutationFn: async (files: File[]) => {
      const images = [...designImages];
      const issues: string[] = [];
      for (const file of files) {
        try {
          const image = await api.uploadDesignImage(file);
          if (images.some((item) => item.sha256 === image.sha256)) continue;
          if (images.some((item) => item.name === image.name)) {
            issues.push(`${file.name} 파일명이 이미 사용 중입니다.`);
            continue;
          }
          images.push(image);
        } catch (error) {
          issues.push(
            `${file.name} · ${error instanceof ApiError ? error.message : "첨부하지 못했습니다."}`,
          );
        }
      }
      return { images, issues };
    },
    onSuccess: ({ images, issues }) => {
      setDesignImages(images);
      if (issues.length) setLocalError(issues.join(" "));
    },
  });
  const preview = useMutation({
    mutationFn: async (proposal: Schema["Proposal"]) => ({
      proposal,
      receipt: await api.preview(proposal),
      key: crypto.randomUUID(),
    }),
    onSuccess: () => setPreviewOpen(true),
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
    onSuccess: (value) => {
      clearDraft();
      navigate(`/runs/${value.run_id}`);
    },
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
    if (!project || imageUpload.isPending) return;
    setLocalError("");
    if (!name.trim()) {
      setLocalError("작업명을 입력해 주세요.");
      return;
    }
    const prefix = commitPrefix.trim();
    if (prefix.includes("[") || prefix.includes("]")) {
      setLocalError("커밋 메시지 접두사에는 대괄호 안의 값만 입력해 주세요.");
      return;
    }
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
      agent_settings: agentSettings,
      name: name.trim(),
      project_id: project,
      spec:
        mode === "text"
          ? { kind: "text", text }
          : { kind: "stored", spec_id: upload.data!.id },
      additional_instructions: instructions,
      ...(designImages.length ? { design_images: designImages } : {}),
      test_scope: testScope,
      auto_approve_plan: autoApprovePlan,
      run_tests: true,
      run_review: true,
      ...(base ? { base_branch: base } : {}),
      ...(branch ? { feature_branch: branch } : {}),
      ...(prefix ? { commit_prefix: prefix } : {}),
      ...(budget !== "" ? { max_fix_attempts: Number(budget) } : {}),
    };
    preview.mutate(proposal);
  }
  const choices = projects.data?.pages.flatMap((p) => p.items) ?? [];
  const validationCommands = Object.entries(
    preview.data?.receipt.resolved_validation?.commands ??
      selected.data?.policy.tests.commands ??
      {},
  );
  const testCount = validationCommands.filter(
    ([, command]) => (command.kind ?? "test") === "test",
  ).length;
  const deferredValidation =
    !!preview.data?.receipt.resolved_validation && !validationCommands.length;
  return (
    <>
      <Link className="back-link" to="/">
        <ArrowLeft size={14} />
        작업 목록으로
      </Link>
      <Heading
        eyebrow="WORKSPACE / NEW RUN"
        title="새 작업"
        description="요구사항과 정책을 확인하고, 격리된 작업 공간에서 실행합니다."
      />
      <p className="help">
        입력 중인 초안은 로그인한 동안 화면을 이동해도 유지됩니다. 새로고침이나
        로그아웃 시에는 사라집니다. 파일은 돌아온 뒤 다시 선택해 주세요.
      </p>
      {projects.isSuccess && !choices.length && (
        <section className="notice">
          <div>
            <strong>먼저 프로젝트를 등록해 주세요</strong>
            <p>작업을 실행할 로컬 Git 저장소가 필요합니다.</p>
            <Button asChild>
              <Link to="/projects">프로젝트 등록하러 가기</Link>
            </Button>
          </div>
        </section>
      )}
      <div className="new-run-grid">
        <section className="panel panel-body">
          <h2>
            <FileText size={18} />
            작업 요구사항
          </h2>
          <form onSubmit={check} className="form-grid">
            <fieldset disabled={locked || preview.isPending}>
              <label>
                작업명
                <input
                  required
                  maxLength={100}
                  value={name}
                  placeholder="예: 로그인 화면 개선"
                  onChange={(e) => edit(() => setName(e.target.value))}
                />
              </label>
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
              <ProjectGitConnection connection={gitConnection} />
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
              <div className="prd-label-row">
                <strong>요구사항</strong>
                <PrdGuide />
              </div>
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
              <div className="design-image-field">
                <label>
                  디자인 참고 이미지 (선택)
                  <input
                    type="file"
                    accept=".png,image/png"
                    multiple
                    disabled={imageUpload.isPending || preview.isPending}
                    aria-describedby="design-images-guide"
                    onChange={(event) => {
                      const files = Array.from(event.currentTarget.files ?? []);
                      event.currentTarget.value = "";
                      if (!files.length) return;
                      edit(() => {
                        if (
                          designImages.length + files.length >
                          maxDesignImages
                        ) {
                          setLocalError(
                            "디자인 참고 이미지는 최대 8장까지 첨부할 수 있습니다.",
                          );
                        } else if (
                          files.some(
                            (file) =>
                              !/\.png$/i.test(file.name) ||
                              !file.size ||
                              file.size > maxDesignImageBytes,
                          )
                        ) {
                          setLocalError(
                            "5 MiB 이하의 PNG 파일을 선택해 주세요.",
                          );
                        } else {
                          imageUpload.mutate(files);
                        }
                      });
                    }}
                  />
                </label>
                <p id="design-images-guide" className="help">
                  PNG 최대 8장, 한 장당 5 MiB. 화면과 이미지 안의 설명을 PRD와
                  함께 에이전트에 전달합니다.
                </p>
                {imageUpload.isPending && (
                  <p role="status">이미지 업로드 중…</p>
                )}
                <DesignImageList
                  images={stagedImages(designImages)}
                  disabled={imageUpload.isPending || preview.isPending}
                  onRemove={(src) =>
                    edit(() =>
                      setDesignImages(
                        designImages.filter(
                          (image) => api.stagedDesignImageUrl(image.id) !== src,
                        ),
                      ),
                    )
                  }
                />
              </div>
              <details>
                <summary>브랜치·추가 지시사항 (선택)</summary>
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
                      직접 입력할 때는{" "}
                      <code>{selected.data.branch_prefix}/</code>로 시작해야
                      합니다. 예:{" "}
                      <code>{selected.data.branch_prefix}/add-task-search</code>
                    </>
                  )}
                </p>
                <label>
                  커밋 메시지 접두사 (선택)
                  <input
                    value={commitPrefix}
                    maxLength={80}
                    aria-describedby="commit-prefix-guide"
                    placeholder="PROJ-123"
                    onChange={(e) =>
                      edit(() => setCommitPrefix(e.target.value))
                    }
                  />
                </label>
                <p id="commit-prefix-guide" className="help">
                  대괄호 안에 들어갈 값만 입력하세요. 비워 두면 기존 브랜치 이름
                  규칙을 사용합니다.
                </p>
                <label>
                  추가 지시사항 (선택)
                  <textarea
                    rows={3}
                    value={instructions}
                    maxLength={65536}
                    onChange={(e) =>
                      edit(() => setInstructions(e.target.value))
                    }
                  />
                </label>
              </details>
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
                  <option value="affected">변경 영향 범위 테스트</option>
                  <option value="full">전체 테스트</option>
                </select>
              </label>
              <p className="help">
                생성 후에는 범위를 변경할 수 없습니다. 모델이 변경 코드와 관련
                테스트를 분석해 자동 선택합니다. 필요한 경우 관련 모듈로 범위를
                확대하고 이유를 기록합니다.
              </p>
              <label className="check-row">
                <input
                  type="checkbox"
                  checked={autoApprovePlan}
                  aria-describedby="auto-approve-plan-guide"
                  onChange={(e) =>
                    edit(() => setAutoApprovePlan(e.target.checked))
                  }
                />
                기획 자동 승인
              </label>
              <p id="auto-approve-plan-guide" className="help">
                기획안이 확정되면 바로 개발을 시작합니다. 최종 승인은 직접
                진행합니다.
              </p>
              <details>
                <summary>모델·자동 수정 설정 (선택)</summary>
                <RunAgentSettings
                  value={agentSettings}
                  onChange={(value) => edit(() => setAgentSettings(value))}
                />
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
              </details>
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
            <div className="form-action-bar">
              <span className="help">
                실행 전에 브랜치와 테스트 범위, 기획 승인 방식을 확인합니다.
              </span>
              <Button
                type="submit"
                disabled={
                  locked ||
                  preview.isPending ||
                  !project ||
                  upload.isPending ||
                  imageUpload.isPending ||
                  !!selected.data?.archived_at ||
                  !!selected.error
                }
              >
                실행 전 확인
                <ArrowRight size={16} />
              </Button>
            </div>
          </form>
        </section>
        <aside>
          <section className="panel panel-body policy-summary">
            <p className="eyebrow">EXECUTION POLICY</p>
            <h2>작업 순서</h2>
            {selected.data && (
              <section>
                <h3>검증 설정</h3>
                <p className="help">
                  {validationCommands.length
                    ? `테스트 ${testCount}개 · 빌드·정적 검사 ${validationCommands.length - testCount}개`
                    : "자동 설정 · 기존 검증을 탐지하고, 구성된 검증이 없으면 개발 후 후보 커밋에서 명령을 확정합니다."}
                </p>
                {validationCommands.length > 0 && (
                  <details key={selected.data.id}>
                    <summary>
                      검증 명령 {validationCommands.length}개 자세히 보기
                    </summary>
                    <div
                      className="new-run-validation-commands"
                      tabIndex={0}
                      role="region"
                      aria-label="검증 명령 목록"
                    >
                      {validationCommands.map(([id, command]) => (
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
                      ))}
                    </div>
                  </details>
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
        </aside>
      </div>
      {preview.data && (
        <Modal
          open={previewOpen}
          onOpenChange={(open) => {
            if (!locked) setPreviewOpen(open);
          }}
          title="실행 미리보기"
          description="브랜치와 테스트 범위, 기획 승인 방식을 확인한 뒤 작업을 시작하세요."
        >
          <dl>
            <dt>작업명</dt>
            <dd>{preview.data.proposal.name}</dd>
            <dt>프로젝트</dt>
            <dd>{selected.data?.name}</dd>
            <dt>Feature 브랜치</dt>
            <dd className="mono path">{preview.data.receipt.branch}</dd>
            {preview.data.proposal.commit_prefix && (
              <>
                <dt>커밋 메시지 접두사</dt>
                <dd className="mono path">
                  [{preview.data.proposal.commit_prefix}]
                </dd>
              </>
            )}
            <dt>기준 커밋</dt>
            <dd className="mono path">{preview.data.receipt.base_sha}</dd>
            {!!preview.data.receipt.design_images?.length && (
              <>
                <dt>디자인 참고 이미지</dt>
                <dd>
                  <DesignImageList
                    images={stagedImages(preview.data.receipt.design_images)}
                  />
                </dd>
              </>
            )}
            {preview.data.receipt.agent_settings && (
              <>
                <dt>에이전트 모델·추론 강도·속도</dt>
                <dd>
                  {Object.entries(
                    preview.data.receipt.agent_settings.roles ?? {},
                  ).map(([role, options]) => (
                    <p key={role}>
                      {role}: {options.model ?? "Codex 기본값"} ·{" "}
                      {options.reasoning_effort ?? "Codex 기본값"} ·{" "}
                      {agentSpeedLabel(options.service_tier)}
                    </p>
                  ))}
                </dd>
              </>
            )}
            <dt>테스트 범위</dt>
            <dd>
              {preview.data.proposal.test_scope === "affected"
                ? "변경 영향 범위 테스트"
                : "전체 테스트"}
            </dd>
            <dt>기획 승인</dt>
            <dd>{preview.data.receipt.auto_approve_plan ? "자동" : "수동"}</dd>
            {deferredValidation && (
              <>
                <dt>검증 명령</dt>
                <dd>
                  개발 후 확정 · 후보 커밋의 코드와 테스트 설정을 자동으로
                  탐지합니다.
                </dd>
              </>
            )}
            <dt>최대 수정 횟수</dt>
            <dd>
              {preview.data.proposal.max_fix_attempts ??
                selected.data?.policy.max_fix_attempts ??
                3}
              회
            </dd>
            <dt>유효 시각</dt>
            <dd>
              {new Date(preview.data.receipt.expires * 1000).toLocaleTimeString(
                "ko-KR",
              )}
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
          <p className="help">접수 후 작업 큐에서 실행 순서를 기다립니다.</p>
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
              응답을 받지 못해 접수 여부가 확정되지 않았습니다. 같은 요청 키로
              안전하게 확인합니다.
            </p>
          )}
        </Modal>
      )}
    </>
  );
}
