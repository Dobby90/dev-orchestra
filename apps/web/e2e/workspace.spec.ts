import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { execFileSync } from "node:child_process";
import { readFileSync, writeFileSync } from "node:fs";
const fixture = () =>
  JSON.parse(readFileSync("test-results/fixture.json", "utf8")) as {
    repo: string;
    database: string;
    release: string;
    python: string;
    policy: unknown;
    base_sha: string;
  };
function bootstrap() {
  const f = fixture();
  return execFileSync(
    f.python,
    [
      "-c",
      "from pathlib import Path;import sys;from dev_orchestra.api.security import bootstrap;from dev_orchestra.infrastructure.store import Store;print(bootstrap(Store(Path(sys.argv[1]))))",
      f.database,
    ],
    { encoding: "utf8" },
  ).trim();
}
test("실제 로컬 API: 로그인 → 프로젝트 → 실행 → 재연결 → 승인", async ({
  page,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  const f = fixture();
  await page.goto("/");
  await expect(page.getByLabel("일회용 로그인 코드")).toBeVisible();
  await page.getByLabel("일회용 로그인 코드").fill(bootstrap());
  await page.getByRole("button", { name: "작업 공간 열기" }).click();
  await expect(
    page.getByRole("heading", { name: "개발의 흐름을 한눈에" }),
  ).toBeVisible();
  await page.screenshot({
    path: "test-results/dashboard-dark.png",
    fullPage: true,
  });
  const audit = await new AxeBuilder({ page }).analyze();
  expect(audit.violations).toEqual([]);
  await page.getByRole("link", { name: "프로젝트", exact: true }).click();
  await page
    .getByRole("button", { name: "프로젝트 등록", exact: true })
    .click();
  await page.getByLabel("프로젝트 이름").fill("GUI 임시 프로젝트");
  await page.getByLabel("저장소 절대 경로").fill(f.repo);
  await page
    .getByLabel("테스트·리뷰 정책 (JSON)")
    .fill(JSON.stringify(f.policy, null, 2));
  await page
    .getByRole("button", { name: "프로젝트 등록", exact: true })
    .last()
    .click();
  await expect(
    page.getByRole("heading", { name: "GUI 임시 프로젝트" }),
  ).toBeVisible();
  await page.getByRole("link", { name: /GUI 임시 프로젝트/ }).click();
  await page.getByRole("link", { name: "이 프로젝트에서 실행" }).click();
  await page
    .getByLabel("PRD", { exact: true })
    .fill("기존 덧셈 기능을 테스트하고 리뷰해 주세요.");
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
  await page.getByRole("button", { name: "실행 전 확인" }).click();
  await expect(
    page.getByRole("heading", { name: "실행 미리보기" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "파일 업로드", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "확인하고 작업 시작" }),
  ).toHaveCount(0);
  await page
    .getByLabel("PRD 파일 (.md / .txt, UTF-8)")
    .setInputFiles({
      name: "prd.md",
      mimeType: "text/markdown",
      buffer: Buffer.from("기존 덧셈 기능을 테스트하고 리뷰해 주세요.", "utf8"),
    });
  await expect(page.getByText(/PRD 저장됨/)).toBeVisible();
  await page.getByRole("button", { name: "실행 전 확인" }).click();
  await expect(
    page.getByRole("heading", { name: "실행 미리보기" }),
  ).toBeVisible();
  const submissions: { body: string | null; key: string | undefined }[] = [];
  await page.route("**/api/runs", async (route) => {
    if (route.request().method() !== "POST") {
      await route.continue();
      return;
    }
    submissions.push({
      body: route.request().postData(),
      key: route.request().headers()["idempotency-key"],
    });
    if (submissions.length === 1) {
      const response = await route.fetch();
      expect(response.status()).toBe(202);
      await route.abort("failed");
    } else await route.continue();
  });
  await page.getByRole("button", { name: "확인하고 작업 시작" }).click();
  await expect(
    page.getByRole("button", { name: "동일 요청으로 접수 확인" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "동일 요청으로 접수 확인" }).click();
  expect(submissions.length).toBe(2);
  expect(submissions[1]).toEqual(submissions[0]);
  await expect(page).toHaveURL(/\/runs\/run-/);
  await expect(page.getByText("개발 중", { exact: true })).toBeVisible({
    timeout: 30000,
  });
  await expect(page.getByText(/GUI 실시간 한글 로그/)).toBeVisible();
  expect(await page.locator("body").innerText()).not.toContain("CANARY");
  const runUrl = page.url();
  await page.route("**/events?*", (route) => route.abort("failed"));
  await page.reload();
  await expect(page).toHaveURL(runUrl);
  await expect(page.getByText("재연결 중 · 주기적 조회")).toBeVisible();
  await expect(page.getByText(/GUI 실시간 한글 로그/)).toBeVisible();
  await page.unroute("**/events?*");
  await expect(page.getByText("실시간 연결됨")).toBeVisible();
  await page.getByRole("button", { name: "일시중지", exact: true }).click();
  await page.getByRole("button", { name: "일시중지 확인" }).click();
  await expect(
    page.getByText("일시중지", { exact: true }).first(),
  ).toBeVisible();
  await expect
    .poll(() =>
      execFileSync(
        f.python,
        [
          "-c",
          "import sqlite3,sys;print(sqlite3.connect(sys.argv[1]).execute(\"SELECT count(*) FROM jobs WHERE state IN ('claimed','running')\").fetchone()[0])",
          f.database,
        ],
        { encoding: "utf8" },
      ).trim(),
    )
    .toBe("0");
  writeFileSync(f.release, "");
  await page.getByRole("button", { name: "재개", exact: true }).click();
  await page.getByRole("button", { name: "작업 재개 확인" }).click();
  await expect(page.getByText("승인 대기", { exact: true })).toBeVisible({
    timeout: 45000,
  });
  await page.getByRole("tab", { name: "테스트", exact: true }).click();
  await expect(
    page.getByText(/상세 결과 API를 제공하지 않습니다/),
  ).toBeVisible();
  await page.getByRole("tab", { name: "최종 보고서" }).click();
  await expect(
    page.getByRole("heading", { name: "DevOrchestra report" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "최종 승인", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "최종 승인 확인" }),
  ).toBeDisabled();
  const sha = await page.locator(".approval-sha").innerText();
  await page.getByLabel("위 커밋 SHA 전체를 입력해 확인하세요").fill(sha);
  await page.getByRole("button", { name: "최종 승인 확인" }).click();
  await expect(page.getByText("완료", { exact: true }).first()).toBeVisible();
  expect(
    execFileSync("git", ["-C", f.repo, "rev-parse", "HEAD"], {
      encoding: "utf8",
    }).trim(),
  ).toBe(f.base_sha);
  expect(
    execFileSync("git", ["-C", f.repo, "status", "--porcelain"], {
      encoding: "utf8",
    }).trim(),
  ).toBe("");
  await page.screenshot({
    path: "test-results/run-completed.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByRole("button", { name: "메뉴 열기" })).toBeVisible();
  await page.getByRole("button", { name: "메뉴 열기" }).click();
  await page.getByRole("link", { name: "대시보드", exact: true }).click();
  await expect(page.getByLabel("주 메뉴")).toBeHidden();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.getByRole("button", { name: "라이트 모드로 전환" }).click();
  await page.screenshot({
    path: "test-results/dashboard-mobile-light.png",
    fullPage: true,
  });
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
  await page.getByRole("button", { name: "메뉴 열기" }).click();
  await page.getByRole("button", { name: "로그아웃", exact: true }).click();
  await expect(page.getByLabel("일회용 로그인 코드")).toBeVisible();
  expect(
    (await page.context().cookies()).some(
      (cookie) => cookie.name === "devorchestra_session",
    ),
  ).toBe(false);
  expect(errors).toEqual([]);
});

test("개발 서버가 검사 산출물과 Python 소스를 제공하지 않는다", async ({
  request,
}) => {
  expect((await request.get("/test-results/fixture.json")).status()).toBe(403);
  const source = new URL(
    "../../../src/dev_orchestra/api/security.py",
    import.meta.url,
  ).pathname;
  expect((await request.get(`/@fs${source}`)).status()).toBe(403);
});
