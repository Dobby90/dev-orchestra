import { useState } from "react";
import { Button } from "./ui/button";
import { Modal } from "./ui/dialog";

const example = `# 목표
브라우저에서 XML을 정리하고 문법 오류를 확인한다.

# 구현 범위
- XML 입력 영역, 포매팅 버튼, 검사 결과 영역을 제공한다.
- 들여쓰기는 공백 2칸으로 정리한다.
- XML 문법만 검사하며 XSD 검증과 파일 업로드는 제외한다.

# 인수 조건
- 정상 XML은 포매팅한 결과를 표시한다.
- 잘못된 XML은 오류 메시지를 표시하고 원문을 유지한다.
- 빈 입력에는 XML을 입력해 달라는 안내를 표시한다.

# 제약 사항
- 기존 화면 구조와 스타일을 따른다.
- XML을 외부 서버로 전송하지 않는다.`;

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
        <h3>XML 기능 작성 예시</h3>
        <p className="help">
          아래는 작성 예시입니다. 원하는 기능에 맞게 범위와 조건을 바꿔 주세요.
        </p>
        <pre>{example}</pre>
      </div>
    </Modal>
  );
}
