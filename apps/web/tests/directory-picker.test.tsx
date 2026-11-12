import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { expect, it, vi } from "vitest";
import { api } from "../src/api/resources";
import { DirectoryPicker } from "../src/components/DirectoryPicker";

it("폴더 이동·선택을 지원하고 취소하면 입력을 변경하지 않는다", async () => {
  vi.spyOn(api, "directories").mockImplementation(async (path) => ({
    path: path || "/projects",
    parent: "/",
    items:
      path === "/projects/example"
        ? []
        : [{ name: "example", path: "/projects/example" }],
    truncated: false,
  }));
  const select = vi.fn();
  render(
    <QueryClientProvider client={new QueryClient()}>
      <DirectoryPicker initialPath="/projects" onSelect={select} />
    </QueryClientProvider>,
  );
  await userEvent.click(screen.getByRole("button", { name: "폴더 선택" }));
  await userEvent.click(await screen.findByRole("button", { name: "example" }));
  await screen.findByText("표시할 하위 폴더가 없습니다.");
  await userEvent.click(screen.getByRole("button", { name: "이 폴더 선택" }));
  expect(select).toHaveBeenCalledWith("/projects/example");
  select.mockClear();
  await userEvent.click(screen.getByRole("button", { name: "폴더 선택" }));
  await userEvent.click(screen.getByRole("button", { name: "대화상자 닫기" }));
  expect(select).not.toHaveBeenCalled();
});
