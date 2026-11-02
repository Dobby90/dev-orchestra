import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowUpRight, FolderGit2, GitBranch, Plus } from "lucide-react";
import { api } from "../api/resources";
import { Heading, ErrorNotice, Loading, Empty } from "../components/common";
import { Button } from "../components/ui/button";
import { Modal } from "../components/ui/dialog";
import { ProjectForm } from "../components/ProjectForm";
export function ProjectsPage() {
  const [open, setOpen] = useState(false),
    [params, setParams] = useSearchParams();
  const cursor = params.get("cursor") ?? "";
  const client = useQueryClient();
  const projects = useQuery({
    queryKey: ["projects", cursor],
    queryFn: () => api.projects(cursor),
  });
  const create = useMutation({
    mutationFn: api.createProject,
    onSuccess: () => {
      client.invalidateQueries({ queryKey: ["projects"] });
      client.invalidateQueries({ queryKey: ["dashboard"] });
      setOpen(false);
    },
  });
  return (
    <>
      <Heading
        eyebrow="WORKSPACE / REPOSITORIES"
        title="프로젝트"
        description="로컬 Git 저장소와 실행 정책을 관리합니다."
      >
        <Button
          onClick={() => {
            create.reset();
            setOpen(true);
          }}
        >
          <Plus size={16} />
          프로젝트 등록
        </Button>
      </Heading>
      {projects.isPending ? (
        <Loading />
      ) : projects.error ? (
        <ErrorNotice error={projects.error} retry={() => projects.refetch()} />
      ) : projects.data.items.length ? (
        <div className="project-grid">
          {projects.data.items.map((p) => (
            <Link className="project-card" key={p.id} to={`/projects/${p.id}`}>
              <div className="project-card-top">
                <FolderGit2 size={24} />
                <ArrowUpRight size={17} />
              </div>
              <h2>{p.name}</h2>
              <p className="mono subtle text-small path">{p.repository_path}</p>
              <div className="project-card-bottom">
                <span>
                  <GitBranch size={14} />
                  {p.default_base_branch}
                </span>
                <span>최대 수정 {p.policy.max_fix_attempts ?? 3}회</span>
              </div>
            </Link>
          ))}
        </div>
      ) : (
        <section className="panel">
          <Empty title="첫 프로젝트를 등록해 보세요">
            API에서 허용한 경로 아래의 Git 저장소를 등록할 수 있습니다.
          </Empty>
        </section>
      )}
      <div className="inline-actions">
        {cursor && (
          <Button variant="ghost" onClick={() => setParams({})}>
            처음으로
          </Button>
        )}
        {projects.data?.has_more && (
          <Button
            variant="secondary"
            onClick={() => setParams({ cursor: projects.data.next_cursor! })}
          >
            다음 프로젝트
          </Button>
        )}
      </div>
      <Modal
        open={open}
        onOpenChange={setOpen}
        title="프로젝트 등록"
        description="허용된 경로의 Git 저장소와 테스트 정책을 입력하세요."
      >
        {create.error && <ErrorNotice error={create.error} />}
        <ProjectForm
          pending={create.isPending}
          onSubmit={(body) => create.mutate(body)}
        />
      </Modal>
    </>
  );
}
