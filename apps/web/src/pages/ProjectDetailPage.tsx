import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Archive, Plus } from "lucide-react";
import { api } from "../api/resources";
import { Heading, ErrorNotice, Loading } from "../components/common";
import { Button } from "../components/ui/button";
import { Modal } from "../components/ui/dialog";
import { ProjectForm } from "../components/ProjectForm";
export function ProjectDetailPage() {
  const { id = "" } = useParams();
  const navigate = useNavigate(),
    client = useQueryClient();
  const [archive, setArchive] = useState(false);
  const project = useQuery({
    queryKey: ["project", id],
    queryFn: () => api.project(id),
  });
  const save = useMutation({
    mutationFn: (body: Parameters<typeof api.patchProject>[1]) =>
      api.patchProject(project.data!, body),
    onSuccess: (value) => {
      client.setQueryData(["project", id], value);
      client.invalidateQueries({ queryKey: ["projects"] });
    },
    onError: () => client.invalidateQueries({ queryKey: ["project", id] }),
  });
  const remove = useMutation({
    mutationFn: () => api.archiveProject(project.data!),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: ["projects"] });
      client.invalidateQueries({ queryKey: ["dashboard"] });
      navigate("/projects");
    },
    onError: () => client.invalidateQueries({ queryKey: ["project", id] }),
  });
  if (project.isPending) return <Loading />;
  if (project.error)
    return (
      <ErrorNotice error={project.error} retry={() => project.refetch()} />
    );
  const p = project.data;
  return (
    <>
      <Link className="back-link" to="/projects">
        <ArrowLeft size={14} />
        프로젝트 목록으로
      </Link>
      <Heading
        eyebrow="PROJECT / SETTINGS"
        title={p.name}
        description={p.repository_path}
      >
        <Button asChild>
          <Link to={`/runs/new?project=${p.id}`}>
            <Plus size={16} />이 프로젝트에서 실행
          </Link>
        </Button>
      </Heading>
      {p.archived_at ? (
        <p className="notice">등록 해제된 프로젝트입니다.</p>
      ) : (
        <div className="detail-grid">
          <section className="panel panel-body">
            <h2>실행 정책</h2>
            <p className="subtle">
              현재 버전을 확인한 뒤 변경 사항을 저장합니다.
            </p>
            {save.error && <ErrorNotice error={save.error} />}
            <ProjectForm
              key={p.version}
              project={p}
              pending={save.isPending}
              onSubmit={({ name, default_base_branch, policy }) =>
                save.mutate({ name, default_base_branch, policy })
              }
            />
            {save.isSuccess && (
              <p role="status" className="text-green">
                변경 사항을 저장했습니다.
              </p>
            )}
          </section>
          <aside className="panel panel-body side-panel">
            <h2>프로젝트 등록 해제</h2>
            <p className="subtle">
              등록 정보를 보관 처리하며 기존 실행 이력을 유지합니다.
            </p>
            <Button variant="danger" onClick={() => setArchive(true)}>
              <Archive size={15} />
              등록 해제
            </Button>
          </aside>
        </div>
      )}
      <Modal
        open={archive}
        onOpenChange={setArchive}
        title="프로젝트 등록을 해제할까요?"
        description="등록 정보를 보관 처리합니다. 원본 저장소와 기존 작업은 유지됩니다."
      >
        {remove.error && <ErrorNotice error={remove.error} />}
        <Button
          variant="danger"
          disabled={remove.isPending}
          onClick={() => remove.mutate()}
        >
          등록 해제 확인
        </Button>
      </Modal>
    </>
  );
}
