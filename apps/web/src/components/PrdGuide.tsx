import { useState } from "react";
import { Button } from "./ui/button";
import { Modal } from "./ui/dialog";

const example = `# 목표
사용자가 할 일 목록에서 제목으로 원하는 항목을 찾을 수 있게 한다.

# 구현 범위
- 기존 할 일 목록 화면에 검색 입력란을 추가한다.
- 검색어가 제목에 포함된 항목만 표시한다.
- 정렬, 태그 검색, 새 항목 추가 기능은 이번 작업에서 제외한다.

# 인수 조건
- 검색어 앞뒤 공백은 제거하고 영문 대소문자는 구분하지 않는다.
- 검색어가 비어 있으면 전체 목록을 표시한다.
- 검색 결과가 없으면 결과가 없다는 안내를 표시한다.
- 목록을 불러오지 못하면 오류 메시지와 재시도 버튼을 표시한다.

# 제약 사항
- 기존 화면 구조와 스타일을 따른다.
- 기존 항목의 추가·수정·삭제 동작을 유지한다.`;

export function PrdGuide() {
  const [open, setOpen] = useState(false);
  return (
    <Modal
      open={open}
      onOpenChange={setOpen}
      trigger={
        <Button type="button" variant="secondary">
          PRD 작성 가이드
        </Button>
      }
      title="PRD 작성 가이드"
      description="PRD는 만들 기능과 완료 조건을 설명하는 요구사항 문서입니다. 고정 양식은 없으며 한 줄로도 입력할 수 있습니다."
    >
      <div className="input-guide">
        <dl>
          <dt>입력 형식</dt>
          <dd>
            직접 텍스트를 작성하거나 UTF-8 .md·.txt 파일을 업로드하세요. 최대 1
            MiB이며 공백만 있는 내용은 사용할 수 없습니다.
          </dd>
          <dt>구체적으로 적으면 좋은 내용</dt>
          <dd>
            목표, 화면 또는 CLI 등 사용 방식, 입력과 출력, 오류 처리, 구현에서
            제외할 범위, 완료를 판단할 인수 조건을 적어 주세요. 짧은
            요구사항에서 중요한 내용이 모호하면 실행 중 추가 확인을 요청할 수
            있습니다.
          </dd>
          <dt>프로젝트 정책과의 관계</dt>
          <dd>
            테스트 실행 명령과 허용 경로는 프로젝트 정책에서 설정합니다. PRD에
            테스트 생략이나 승인 우회를 적어도 호스트 정책을 바꿀 수 없습니다.
          </dd>
        </dl>
        <h3>일반적인 기능 개발 예시</h3>
        <p className="help">
          아래는 작성 예시입니다. 원하는 기능에 맞게 범위와 조건을 바꿔 주세요.
        </p>
        <pre>{example}</pre>
      </div>
    </Modal>
  );
}
