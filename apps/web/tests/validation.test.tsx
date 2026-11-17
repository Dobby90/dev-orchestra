import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { QueryClientProvider } from "@tanstack/react-query";
import { expect, it, vi } from "vitest";
import { ProjectForm, initialPolicy } from "../src/components/ProjectForm";
import { api } from "../src/api/resources";
import { createQueryClient } from "../src/App";
import type { Schema } from "../src/api/types";

const unit: Schema["ValidationCandidate"] = {
  id: "node-test_1",
  label: ". · test",
  evidence: ["package.json"],
  warnings: [],
  command: {
    kind: "test",
    argv: ["npm", "test"],
    junit_report: "build/unit.xml",
    timeout_seconds: 600,
    working_directory: ".",
    network: "none",
  },
  write_paths: ["tests"],
  output_paths: ["build"],
};
function mount(onSubmit = vi.fn()) {
  render(
    <QueryClientProvider client={createQueryClient()}>
      <MemoryRouter>
        <ProjectForm pending={false} onSubmit={onSubmit} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return onSubmit;
}

it("자동 설정으로 등록할 때 수동 감지나 도구 선택이 필요하지 않다", async () => {
  const profiles = vi.spyOn(api, "validationProfiles");
  const submit = mount();
  await userEvent.type(screen.getByLabelText("프로젝트 이름"), "자동 프로젝트");
  await userEvent.type(
    screen.getByLabelText("저장소 절대 경로"),
    "/tmp/example",
  );
  await userEvent.click(screen.getByText("고급 설정 · 검증 직접 설정"));
  expect(
    screen.queryByRole("button", { name: /감지/ }),
  ).not.toBeInTheDocument();
  expect(screen.queryByText("감지된 검증 후보")).not.toBeInTheDocument();
  expect(profiles).not.toHaveBeenCalled();
  await userEvent.click(screen.getByRole("button", { name: "프로젝트 등록" }));
  expect(submit).toHaveBeenCalledWith(
    expect.objectContaining({
      policy: expect.objectContaining({
        tests: { commands: {}, write_paths: [], output_paths: [] },
      }),
    }),
  );
});

it("프로젝트 수정에서 기존 직접 설정을 유지하고 자동 설정으로 전환할 수 있다", async () => {
  const submit = vi.fn();
  render(
    <MemoryRouter>
      <ProjectForm
        pending={false}
        project={
          {
            id: "p1",
            name: "기존 프로젝트",
            repository_path: "/tmp/example",
            default_base_branch: "main",
            policy: initialPolicy,
            version: 1,
          } as Schema["ProjectView"]
        }
        onSubmit={submit}
      />
    </MemoryRouter>,
  );
  expect(
    screen.queryByRole("button", { name: /감지/ }),
  ).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "변경 사항 저장" }));
  expect(submit).toHaveBeenLastCalledWith(
    expect.objectContaining({ policy: initialPolicy }),
  );
  await userEvent.click(screen.getByText("고급 설정 · 검증 직접 설정"));
  await userEvent.click(screen.getByRole("button", { name: "자동 설정 사용" }));
  await userEvent.click(screen.getByRole("button", { name: "변경 사항 저장" }));
  expect(submit).toHaveBeenLastCalledWith(
    expect.objectContaining({
      policy: expect.objectContaining({
        tests: { commands: {}, write_paths: [], output_paths: [] },
      }),
    }),
  );
});

it("Java 도구를 직접 선택하고 명령·보고서 경로를 폼으로 수정한다", async () => {
  const maven: Schema["ValidationCandidate"] = {
    ...unit,
    id: "maven",
    label: "Java · Maven 테스트",
    command: {
      ...unit.command,
      argv: ["mvn", "-o", "test"],
      junit_report: "target/surefire-reports/TEST-*.xml",
    },
    write_paths: ["src/test"],
    output_paths: ["target"],
  };
  vi.spyOn(api, "validationProfiles").mockResolvedValue([maven]);
  const submit = mount();
  await userEvent.type(screen.getByLabelText("프로젝트 이름"), "Java 프로젝트");
  await userEvent.type(
    screen.getByLabelText("저장소 절대 경로"),
    "/tmp/example",
  );
  await userEvent.click(screen.getByText("고급 설정 · 검증 직접 설정"));
  await userEvent.click(screen.getByRole("button", { name: "도구 직접 선택" }));
  await userEvent.selectOptions(
    await screen.findByRole("combobox", { name: /추가할 검증 도구/ }),
    "maven",
  );
  await userEvent.click(screen.getByText("테스트 · mvn -o test"));
  await userEvent.clear(screen.getByLabelText("실행 파일 (maven)"));
  await userEvent.type(screen.getByLabelText("실행 파일 (maven)"), "./mvnw");
  await userEvent.click(screen.getByRole("button", { name: "프로젝트 등록" }));
  expect(submit).toHaveBeenCalledWith(
    expect.objectContaining({
      policy: expect.objectContaining({
        tests: expect.objectContaining({
          commands: {
            maven: { ...maven.command, argv: ["./mvnw", "-o", "test"] },
          },
          write_paths: ["src/test"],
        }),
      }),
    }),
  );
  expect(screen.getByText(/기존 작업의 재개/)).toBeVisible();
});

it("자동 설정으로 등록하고 고급 JSON 편집도 유지한다", async () => {
  const submit = mount();
  await userEvent.type(screen.getByLabelText("프로젝트 이름"), "프로젝트");
  await userEvent.type(
    screen.getByLabelText("저장소 절대 경로"),
    "/tmp/example",
  );
  await userEvent.click(screen.getByRole("button", { name: "프로젝트 등록" }));
  expect(submit).toHaveBeenCalledWith(
    expect.objectContaining({
      policy: expect.objectContaining({
        tests: { commands: {}, write_paths: [], output_paths: [] },
      }),
    }),
  );
  submit.mockClear();
  await userEvent.click(screen.getByText("고급 설정 · 정책 JSON 직접 편집"));
  fireEvent.change(screen.getByLabelText("테스트·리뷰 정책 (JSON)"), {
    target: { value: JSON.stringify(initialPolicy) },
  });
  await userEvent.click(screen.getByRole("button", { name: "프로젝트 등록" }));
  await waitFor(() => expect(submit).toHaveBeenCalled());
});

it("고급 설정에 잘못된 구조를 입력해도 화면을 유지하고 제출을 차단한다", async () => {
  const submit = mount();
  await userEvent.type(screen.getByLabelText("프로젝트 이름"), "프로젝트");
  await userEvent.type(
    screen.getByLabelText("저장소 절대 경로"),
    "/tmp/example",
  );
  await userEvent.click(screen.getByText("고급 설정 · 정책 JSON 직접 편집"));
  fireEvent.change(screen.getByLabelText("테스트·리뷰 정책 (JSON)"), {
    target: {
      value: '{"tests":{"commands":{"unit":null}},"blocking_severities":[]}',
    },
  });
  await userEvent.click(screen.getByRole("button", { name: "프로젝트 등록" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "유효한 JSON 객체",
  );
  expect(submit).not.toHaveBeenCalled();
});

it("적용한 검증 정책은 경로 변경 후 재확인해야 제출한다", async () => {
  const submit = mount();
  await userEvent.type(screen.getByLabelText("프로젝트 이름"), "프로젝트");
  await userEvent.type(screen.getByLabelText("저장소 절대 경로"), "/tmp/a");
  await userEvent.click(screen.getByText("고급 설정 · 정책 JSON 직접 편집"));
  fireEvent.change(screen.getByLabelText("테스트·리뷰 정책 (JSON)"), {
    target: { value: JSON.stringify(initialPolicy) },
  });
  await userEvent.type(screen.getByLabelText("저장소 절대 경로"), "b");
  await userEvent.click(screen.getByRole("button", { name: "프로젝트 등록" }));
  expect(submit).not.toHaveBeenCalled();
  expect(screen.getByRole("alert")).toHaveTextContent("재확인");
  await userEvent.click(
    screen.getByRole("checkbox", { name: /변경한 저장소/ }),
  );
  await userEvent.click(screen.getByRole("button", { name: "프로젝트 등록" }));
  expect(submit).toHaveBeenCalledOnce();
});
