import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { execFileSync } from "node:child_process";
import { existsSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
const fixture = () =>
  JSON.parse(readFileSync("test-results/fixture.json", "utf8")) as {
    repo: string;
    database: string;
    release: string;
    python: string;
    config: string;
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
  const loginStarted = Date.now() / 1000;
  await page.getByRole("button", { name: "작업 공간 열기" }).click();
  await expect(
    page.getByRole("heading", { name: "개발의 흐름을 한눈에" }),
  ).toBeVisible();
  const sessionCookie = (await page.context().cookies()).find(
    (cookie) => cookie.name === "devorchestra_session",
  );
  expect(sessionCookie).toBeDefined();
  expect(sessionCookie!.httpOnly).toBe(true);
  expect(sessionCookie!.sameSite).toBe("Strict");
  expect(sessionCookie!.path).toBe("/api");
  expect(sessionCookie!.expires).toBeGreaterThanOrEqual(
    loginStarted + 172800 - 5,
  );
  expect(sessionCookie!.expires).toBeLessThanOrEqual(
    Date.now() / 1000 + 172800 + 5,
  );
  await page.screenshot({
    path: "test-results/dashboard-dark.png",
    fullPage: true,
  });
  const audit = await new AxeBuilder({ page }).analyze();
  expect(audit.violations).toEqual([]);
  await page.getByRole("link", { name: "연결 및 환경", exact: true }).click();
  await page.getByRole("button", { name: "모델 목록 불러오기" }).click();
  await page
    .getByRole("combobox", { name: "공통 기본값 모델", exact: true })
    .selectOption("test-standard");
  await page
    .getByRole("combobox", { name: "공통 기본값 추론 강도", exact: true })
    .selectOption("medium");
  await page
    .getByRole("combobox", { name: "Review 모델", exact: true })
    .selectOption("test-review");
  await page
    .getByRole("combobox", { name: "Review 추론 강도", exact: true })
    .selectOption("xhigh");
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
  await page.getByRole("button", { name: "에이전트 설정 저장" }).click();
  await expect(page.getByText("에이전트 설정을 저장했습니다.")).toBeVisible();
  await page.screenshot({
    path: "test-results/agent-settings.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
  await page.screenshot({
    path: "test-results/agent-settings-mobile.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.reload();
  await expect(
    page.getByRole("combobox", { name: "Review 모델", exact: true }),
  ).toHaveValue("test-review");
  await page.getByRole("link", { name: "프로젝트", exact: true }).click();
  await page
    .getByRole("button", { name: "프로젝트 등록", exact: true })
    .click();
  await page.getByLabel("프로젝트 이름").fill("GUI 임시 프로젝트");
  await page.getByLabel("저장소 절대 경로").fill(f.repo);
  await page.getByText("고급 설정 · 정책 JSON 직접 편집").click();
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
  await page.getByLabel("PRD 파일 (.md / .txt, UTF-8)").setInputFiles({
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
  await page.getByRole("tab", { name: "에이전트·사용량", exact: true }).click();
  await expect(
    page.getByText("실행에 저장된 모델 설정", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("cell", { name: "test-review", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("cell", { name: "xhigh", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("일부 집계", { exact: true }).first(),
  ).toBeVisible();
  await page.screenshot({
    path: "test-results/agent-usage.png",
    fullPage: true,
  });
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
  await page.getByRole("tab", { name: "테스트", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "테스트 통과", exact: true }),
  ).toBeVisible();
  const junitDownload = page.waitForEvent("download");
  await page.getByRole("link", { name: "보존된 JUnit" }).click();
  const junit = await junitDownload;
  expect(readFileSync((await junit.path())!, "utf8")).toContain("testcase");
  await page.getByRole("tab", { name: "코드 리뷰", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "독립 코드 리뷰" }),
  ).toBeVisible();
  await page.getByRole("tab", { name: "변경 내용", exact: true }).click();
  await expect(page.getByText("이 후보의 변경 파일이 없습니다")).toBeVisible();
  await page.getByRole("button", { name: "보류", exact: true }).click();
  await page.getByLabel("사유 (필수)").fill("추가 확인 뒤 승인합니다");
  await page.getByRole("button", { name: "승인 보류 확인" }).click();
  await expect(page.getByText("승인 대기", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "수정 요청", exact: true }).click();
  await page
    .getByLabel("수정 지시사항")
    .fill("변경 없이도 테스트와 리뷰를 다시 검증해 주세요.");
  await page.getByRole("button", { name: "수정 요청 확인" }).click();
  await expect(page.getByText("승인 대기", { exact: true })).toBeVisible({
    timeout: 45000,
  });
  await expect(page.locator(".validation-strip")).toContainText("revision 1");
  await page.getByRole("tab", { name: "테스트", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "테스트 통과", exact: true }),
  ).toBeVisible();
  const history = page.getByLabel("결과 회차 선택");
  await expect(history.locator("option")).toHaveCount(3);
  await history.selectOption(
    (await history.locator("option").nth(1).getAttribute("value")) ?? "",
  );
  await expect(page.getByText(/이전 revision 0의 결과입니다/)).toBeVisible();
  await history.selectOption("");
  const snapshot = execFileSync(
    f.python,
    [
      "-c",
      "import sqlite3,json,sys;c=sqlite3.connect(sys.argv[1]);print(json.loads(c.execute('SELECT data FROM runs').fetchone()[0])['revision']);print(c.execute(\"SELECT count(*) FROM steps WHERE role='tester' AND status='completed'\").fetchone()[0])",
      f.database,
    ],
    { encoding: "utf8" },
  );
  expect(snapshot.trim()).toBe("1\n2");
  await page.getByRole("tab", { name: "최종 보고서" }).click();
  await expect(
    page.getByRole("heading", { name: "DevOrchestra report" }),
  ).toBeVisible();
  const rid = new URL(page.url()).pathname.split("/").at(-1)!;
  const cli = join(dirname(f.python), "devorchestra");
  const cliList = JSON.parse(
    execFileSync(cli, ["--config", f.config, "list"], { encoding: "utf8" }),
  );
  expect(cliList.some((run: { id: string }) => run.id === rid)).toBe(true);
  await expect
    .poll(
      () =>
        JSON.parse(
          execFileSync(cli, ["--config", f.config, "status", rid], {
            encoding: "utf8",
          }),
        ).state,
    )
    .toBe("waiting_approval");
  await expect(page.getByText("승인 대기", { exact: true })).toBeVisible();
  const cliLogs = execFileSync(cli, ["--config", f.config, "logs", rid], {
    encoding: "utf8",
  });
  expect(cliLogs).toContain("GUI 실시간 한글 로그");
  expect(cliLogs).not.toContain("CANARY");
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
  await page
    .getByRole("button", { name: "Worktree 정리", exact: true })
    .click();
  await page.getByRole("button", { name: "Worktree 정리 확인" }).click();
  // Real pytest leaves untracked outputs. Cleanup must refuse instead of forcing removal.
  await expect(
    page.getByRole("status").filter({ hasText: "수동 확인 필요" }),
  ).toBeVisible();
  const worktree = execFileSync(
    f.python,
    [
      "-c",
      "import sqlite3,json,sys;print(json.loads(sqlite3.connect(sys.argv[1]).execute('SELECT data FROM runs WHERE id=?',(sys.argv[2],)).fetchone()[0])['worktree'])",
      f.database,
      rid,
    ],
    { encoding: "utf8" },
  ).trim();
  expect(existsSync(worktree)).toBe(true);
  // The test owns this temporary fixture: simulate the user removing only its known outputs.
  expect(worktree.startsWith(dirname(f.database) + "/worktrees/")).toBe(true);
  for (const name of ["build", ".pytest_cache"])
    rmSync(join(worktree, name), { recursive: true });
  await page
    .getByRole("button", { name: "Worktree 정리", exact: true })
    .click();
  await page.getByRole("button", { name: "Worktree 정리 확인" }).click();
  await expect(
    page.getByRole("status").filter({ hasText: "적용됨" }),
  ).toBeVisible();
  await expect.poll(() => existsSync(worktree)).toBe(false);
  await expect(
    page.getByRole("button", { name: "Worktree 정리", exact: true }),
  ).toHaveCount(0);
  await page.getByRole("tab", { name: "변경 내용", exact: true }).click();
  await expect(page.getByText("이 후보의 변경 파일이 없습니다")).toBeVisible();
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
  expect((await request.get("/test-results/fixture.json")).status()).toBe(
    process.env.E2E_BUNDLED ? 404 : 403,
  );
  const source = new URL(
    "../../../src/dev_orchestra/api/security.py",
    import.meta.url,
  ).pathname;
  expect((await request.get(`/@fs${source}`)).status()).toBe(
    process.env.E2E_BUNDLED ? 404 : 403,
  );
});

test("CLI 시작·브라우저 종료·재접속·CLI 승인과 GUI 생성 교차 동기화", async ({
  page,
  context,
}) => {
  const f = fixture();
  const cli = join(dirname(f.python), "devorchestra");
  const cliArgs = ["--config", f.config];
  const repo = join(dirname(f.database), "cross-repository");
  execFileSync("git", [
    "clone",
    "--local",
    "--single-branch",
    "--no-hardlinks",
    f.repo,
    repo,
  ]);
  execFileSync("git", ["-C", repo, "config", "user.name", "교차 검사"]);
  execFileSync("git", ["-C", repo, "config", "user.email", "test@localhost"]);
  rmSync(f.release, { force: true });
  const spec = join(dirname(f.database), "cli-prd.md");
  writeFileSync(spec, "기존 덧셈을 테스트하고 리뷰해 주세요.");
  const output = execFileSync(
    cli,
    [...cliArgs, "run", "--repo", repo, "--spec", spec, "--detach"],
    { encoding: "utf8" },
  );
  const rid = output.match(/run-[0-9a-f]{16}/)![0];
  await page.goto(`/runs/${rid}`);
  await page.getByLabel("일회용 로그인 코드").fill(bootstrap());
  await page.getByRole("button", { name: "작업 공간 열기" }).click();
  await expect(page.getByLabel("일회용 로그인 코드")).toBeHidden();
  await expect(
    page.getByText(
      "CLI 작업 · 조회 전용입니다. 실행 제어는 CLI에서 진행해 주세요.",
    ),
  ).toBeVisible();
  await expect(page.getByText("개발 중", { exact: true })).toBeVisible();
  await expect(
    page.getByRole("button", { name: "최종 승인", exact: true }),
  ).toHaveCount(0);
  await page.close();
  expect(
    JSON.parse(
      execFileSync(cli, [...cliArgs, "status", rid], { encoding: "utf8" }),
    ).state,
  ).toBe("developing");
  const reopened = await context.newPage();
  await reopened.goto(`/runs/${rid}`);
  await expect(reopened.getByText("개발 중", { exact: true })).toBeVisible();
  writeFileSync(f.release, "");
  await expect(reopened.getByText("승인 대기", { exact: true })).toBeVisible({
    timeout: 45000,
  });
  execFileSync(cli, [...cliArgs, "approve", rid]);
  await expect(
    reopened.getByText("완료", { exact: true }).first(),
  ).toBeVisible();
  await reopened.goto("/projects");
  await reopened
    .getByRole("button", { name: "프로젝트 등록", exact: true })
    .click();
  await reopened.getByLabel("프로젝트 이름").fill("독립 교차 프로젝트");
  await reopened.getByLabel("저장소 절대 경로").fill(repo);
  await reopened.getByText("고급 설정 · 정책 JSON 직접 편집").click();
  await reopened
    .getByLabel("테스트·리뷰 정책 (JSON)")
    .fill(JSON.stringify(f.policy));
  await reopened
    .getByRole("button", { name: "프로젝트 등록", exact: true })
    .last()
    .click();
  await expect(
    reopened.getByRole("heading", { name: "독립 교차 프로젝트" }),
  ).toBeVisible();
  await reopened.getByRole("link", { name: /독립 교차 프로젝트/ }).click();
  await reopened.getByRole("link", { name: "이 프로젝트에서 실행" }).click();
  await reopened
    .getByLabel("PRD", { exact: true })
    .fill("기존 덧셈을 다시 검증해 주세요.");
  await reopened.getByRole("button", { name: "실행 전 확인" }).click();
  await reopened.getByRole("button", { name: "확인하고 작업 시작" }).click();
  await expect(reopened).toHaveURL(/\/runs\/run-/);
  await expect(reopened.getByText("승인 대기", { exact: true })).toBeVisible({
    timeout: 45000,
  });
  const guiRid = new URL(reopened.url()).pathname.split("/").at(-1)!;
  expect(guiRid).not.toBe(rid);
  execFileSync(cli, [...cliArgs, "approve", guiRid]);
  await expect(
    reopened.getByText("완료", { exact: true }).first(),
  ).toBeVisible();
  await reopened.getByRole("tab", { name: "최종 보고서" }).click();
  await expect(
    reopened.getByText(/completed/, { exact: false }).first(),
  ).toBeVisible();
  expect(
    execFileSync("git", ["-C", repo, "rev-parse", "HEAD"], {
      encoding: "utf8",
    }).trim(),
  ).toBe(f.base_sha);
});

test("검증 자동 감지: Node·Java 후보를 JSON 편집 없이 선택·저장한다", async ({
  page,
}) => {
  const f = fixture();
  const repo = join(dirname(f.repo), "validation-repository");
  execFileSync(f.python, [
    "-c",
    `
import json,subprocess,sys
from pathlib import Path
repo=Path(sys.argv[1]);repo.mkdir()
(repo/'package.json').write_text(json.dumps({'scripts':{'test':'vitest run','build':'vite build','test:browser':'playwright test'},'devDependencies':{'vitest':'*'}}))
(repo/'service').mkdir();(repo/'service/pom.xml').write_text('<project/>')
for args in [('init','-b','main'),('config','user.name','GUI 검사'),('config','user.email','test@example.invalid'),('add','.'),('commit','-m','검증 감지 기준선')]:
 subprocess.run(['git','-C',str(repo),*args],check=True,capture_output=True)
`,
    repo,
  ]);
  await page.goto("/projects");
  const login = page.getByLabel("일회용 로그인 코드");
  if (await login.isVisible()) {
    await login.fill(bootstrap());
    await page.getByRole("button", { name: "작업 공간 열기" }).click();
  }
  await page
    .getByRole("button", { name: "프로젝트 등록", exact: true })
    .click();
  await page.getByLabel("프로젝트 이름").fill("자동 감지 프로젝트");
  await page.getByLabel("저장소 절대 경로").fill(repo);
  await page.getByRole("button", { name: "검증 자동 감지" }).click();
  await expect(
    page.getByRole("heading", { name: "감지된 검증 후보" }),
  ).toBeVisible();
  await expect(
    page.getByLabel("service · Maven 테스트", { exact: true }),
  ).toBeChecked();
  await expect(
    page.getByLabel(". · test:browser (Playwright)", { exact: true }),
  ).toBeChecked();
  await page.getByRole("button", { name: "선택한 검증 적용" }).click();
  await expect(page.getByText("고급 설정 · 정책 JSON 직접 편집")).toBeVisible();
  expect(
    await page
      .getByText("고급 설정 · 정책 JSON 직접 편집")
      .locator("..")
      .getAttribute("open"),
  ).toBeNull();
  const audit = await new AxeBuilder({ page }).analyze();
  expect(audit.violations).toEqual([]);
  await page.screenshot({
    path: "test-results/validation-selection.png",
    fullPage: true,
  });
  await page
    .getByRole("button", { name: "프로젝트 등록", exact: true })
    .last()
    .click();
  await expect(
    page.getByRole("heading", { name: "자동 감지 프로젝트" }),
  ).toBeVisible();
  await page.getByRole("link", { name: /자동 감지 프로젝트/ }).click();
  await page.getByRole("link", { name: "이 프로젝트에서 실행" }).click();
  await expect(
    page.getByRole("heading", { name: "실행할 검증 명령" }),
  ).toBeVisible();
  await expect(page.getByText(/maven-build/).first()).toBeVisible();
});
