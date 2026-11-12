import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/resources";
import { ErrorNotice, Loading } from "./common";
import { Button } from "./ui/button";
import { Modal } from "./ui/dialog";

export function DirectoryPicker({
  initialPath,
  onSelect,
}: {
  initialPath: string;
  onSelect: (path: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [path, setPath] = useState("");
  const [input, setInput] = useState("");
  const query = useQuery({
    queryKey: ["project-directories", path],
    queryFn: () => api.directories(path),
    enabled: open,
    retry: false,
  });
  function navigate(value: string) {
    setPath(value);
    setInput(value);
  }
  return (
    <>
      <Button
        type="button"
        variant="secondary"
        onClick={() => {
          navigate(initialPath);
          setOpen(true);
        }}
      >
        폴더 선택
      </Button>
      <Modal
        open={open}
        onOpenChange={setOpen}
        title="프로젝트 폴더 선택"
        description="로컬 컴퓨터의 Git 저장소 폴더를 선택하세요. 숨김 폴더와 심볼릭 링크는 표시하지 않습니다."
        footer={
          <Button
            type="button"
            disabled={!query.data || query.isFetching || !!query.error}
            onClick={() => {
              if (query.data) {
                onSelect(query.data.path);
                setOpen(false);
              }
            }}
          >
            이 폴더 선택
          </Button>
        }
      >
        <label>
          이동할 폴더 경로
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                navigate(input);
              }
            }}
          />
        </label>
        <Button
          type="button"
          variant="secondary"
          onClick={() => navigate(input)}
        >
          이동
        </Button>
        {query.isFetching ? (
          <Loading />
        ) : query.error ? (
          <ErrorNotice error={query.error} retry={() => query.refetch()} />
        ) : (
          query.data && (
            <>
              <p className="path mono" aria-live="polite">
                {query.data.path}
              </p>
              {query.data.parent && (
                <Button
                  type="button"
                  variant="secondary"
                  onClick={() => navigate(query.data!.parent!)}
                >
                  상위 폴더
                </Button>
              )}
              <ul aria-label="하위 폴더">
                {query.data.items.map((item) => (
                  <li key={item.path}>
                    <Button
                      type="button"
                      variant="ghost"
                      onClick={() => navigate(item.path)}
                    >
                      {item.name}
                    </Button>
                  </li>
                ))}
              </ul>
              {!query.data.items.length && <p>표시할 하위 폴더가 없습니다.</p>}
              {query.data.truncated && (
                <p>
                  폴더가 많아 일부만 표시합니다. 경로를 직접 입력해 이동하세요.
                </p>
              )}
            </>
          )
        )}
      </Modal>
    </>
  );
}
