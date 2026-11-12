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
const build: Schema["ValidationCandidate"] = {
  ...unit,
  id: "build_2",
  label: ". · build",
  command: {
    ...unit.command,
    kind: "check",
    argv: ["npm", "run", "build"],
    junit_report: null,
  },
  write_paths: [],
  output_paths: ["dist"],
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

it("pytest를 기본 적용하지 않고 감지 결과를 선택해 JSON 편집 없이 저장한다", async () => {
  const detect = vi.spyOn(api, "detectValidation").mockResolvedValue({
    base_sha: "a".repeat(40),
    candidates: [unit, build],
    warnings: [],
  });
  const submit = mount();
  expect(screen.queryByLabelText("실행 파일 (unit)")).not.toBeInTheDocument();
  await userEvent.type(screen.getByLabelText("프로젝트 이름"), "Node 프로젝트");
  await userEvent.type(
    screen.getByLabelText("저장소 절대 경로"),
    "/tmp/example",
  );
  await userEvent.click(screen.getByRole("button", { name: "검증 자동 감지" }));
  expect(
    (await screen.findAllByText(/감지 근거: package.json/))[0],
  ).toBeVisible();
  expect(detect).toHaveBeenCalledWith({
    repository_path: "/tmp/example",
    base_branch: "main",
  });
  expect(
    screen.queryByLabelText("실행 파일 (node-test_1)"),
  ).not.toBeInTheDocument();
  await userEvent.click(
    screen.getByRole("button", { name: "선택한 검증 적용" }),
  );
  expect(screen.getByLabelText("실행 파일 (node-test_1)")).toHaveValue("npm");
  await userEvent.click(screen.getByRole("button", { name: "프로젝트 등록" }));
  expect(submit).toHaveBeenCalledWith(
    expect.objectContaining({
      policy: expect.objectContaining({
        tests: expect.objectContaining({
          commands: { [unit.id]: unit.command, [build.id]: build.command },
          output_paths: ["build", "dist"],
        }),
      }),
    }),
  );
});

it("빌드만 선택하면 적용을 차단하고 저장되지 않은 감지 결과는 경로 변경 시 폐기한다", async () => {
  vi.spyOn(api, "detectValidation").mockResolvedValue({
    base_sha: "a",
    candidates: [unit, build],
    warnings: [],
  });
  mount();
  await userEvent.type(screen.getByLabelText("저장소 절대 경로"), "/tmp/a");
  await userEvent.click(screen.getByRole("button", { name: "검증 자동 감지" }));
  await userEvent.click(await screen.findByLabelText(unit.label));
  expect(
    screen.getByRole("button", { name: "선택한 검증 적용" }),
  ).toBeDisabled();
  await userEvent.type(screen.getByLabelText("저장소 절대 경로"), "b");
  expect(screen.queryByText("감지된 검증 후보")).not.toBeInTheDocument();
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

it("빈 정책은 제출을 막고 고급 JSON 편집은 유지한다", async () => {
  const submit = mount();
  await userEvent.type(screen.getByLabelText("프로젝트 이름"), "프로젝트");
  await userEvent.type(
    screen.getByLabelText("저장소 절대 경로"),
    "/tmp/example",
  );
  await userEvent.click(screen.getByRole("button", { name: "프로젝트 등록" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "테스트 검증을 하나 이상",
  );
  expect(submit).not.toHaveBeenCalled();
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
