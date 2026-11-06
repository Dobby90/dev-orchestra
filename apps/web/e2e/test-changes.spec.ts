import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";

test("기존 테스트 변경: 자동 수정·실제 검증 → 최종 승인", async ({ page }) => {
  const f = JSON.parse(readFileSync("test-results/fixture.json", "utf8")) as {
    repo: string;
    database: string;
    python: string;
    config: string;
    policy: unknown;
  };
  const code = execFileSync(
    f.python,
    [
      "-c",
      "from pathlib import Path;import sys;from dev_orchestra.api.security import bootstrap;from dev_orchestra.infrastructure.store import Store;print(bootstrap(Store(Path(sys.argv[1]))))",
      f.database,
    ],
    { encoding: "utf8" },
  ).trim();
  await page.goto("/");
  await page.getByLabel("일회용 로그인 코드").fill(code);
  await page.getByRole("button", { name: "작업 공간 열기" }).click();
  await expect(page.getByRole("heading", { name: "작업 현황" })).toBeVisible();
  const repo = join(dirname(f.repo), "test-change-repository");
  execFileSync("git", ["clone", "--quiet", f.repo, repo]);
  execFileSync("git", ["-C", repo, "config", "user.name", "GUI 검사"]);
  execFileSync("git", [
    "-C",
    repo,
    "config",
    "user.email",
    "test@example.invalid",
  ]);
  const sessionResponse = await page.request.get("/api/session", {
    headers: { Origin: new URL(page.url()).origin },
  });
  expect(sessionResponse.status()).toBe(200);
  const session = await sessionResponse.json();
  const registered = await page.request.post("/api/projects", {
    headers: {
      "X-CSRF-Token": session.csrf_token,
      Origin: new URL(page.url()).origin,
    },
    data: {
      name: "기존 테스트 변경 검증",
      repository_path: repo,
      policy: f.policy,
    },
  });
  expect(registered.status()).toBe(201);
  const project = await registered.json();
  const pending = JSON.parse(
    execFileSync(
      f.python,
      [
        "-c",
        `
import json,sys
from pathlib import Path
from dev_orchestra.application.service import RunService
from dev_orchestra.config import load_config
from dev_orchestra.domain.models import AgentResult,Criterion,Coverage,ReviewResultV2,WorkspaceResult,WorkspaceTestResult
from dev_orchestra.infrastructure.files import digest
from dev_orchestra.infrastructure.test_runner import TestRunner
from dev_orchestra.orchestration.runner import Runner
repo=Path(sys.argv[2]);spec=repo.parent/'test-change-prd.md';spec.write_text('덧셈의 0 입력 회귀 검사를 추가하세요.')
svc=RunService(load_config(Path(sys.argv[1])));rid=svc.create(repo,spec)
with svc.store.connection() as db:db.execute('UPDATE runs SET project_id=? WHERE id=?',(sys.argv[3],rid))
class Agent:
 def run(self,request,*args):
  if request.role=='planner':return AgentResult(summary='계획',plan='0 입력 회귀 검사 추가',criteria=[Criterion(id='AC1',description='덧셈의 0 입력 검증')])
  if request.role=='tester':return AgentResult(summary='전체 테스트',command_ids=['unit'],coverage=[Coverage(criterion_id='AC1',command_ids=['unit'])])
  if request.role=='reviewer':return ReviewResultV2(summary='기존 검사를 보존하는 요구사항 기반 확장 확인')
 def run_workspace(self,request,directory,session,stop):
  if request.role=='tester':
   value=session.run_tests(session.test_policy()['included'])
   return WorkspaceTestResult(summary='실제 검증',test_evidence_id=value['evidence_id'],command_ids=value['selection']['included'],coverage=[Coverage(criterion_id='AC1',command_ids=['unit'])])
  target=session.root/'tests/test_main.py'
  session.edit_file('tests/test_main.py',digest(target.read_bytes()),target.read_text()+'    assert add(0, 0) == 0\\n',test_change={'file':'tests/test_main.py','criterion_ids':['AC1'],'reason':'기존 덧셈 검사를 보존하고 0 입력 경계값 검사를 추가합니다.'})
  return WorkspaceResult(summary='기존 테스트 확장')
Runner(svc.store,Agent(),TestRunner()).execute(rid)
from dev_orchestra.application.plan_approval import binding,decide
run=svc.store.get(rid);assert run['state']=='waiting_plan_approval',run.get('error')
decide(svc,rid,binding(svc.store,run)['plan_hash'],True)
Runner(svc.store,Agent(),TestRunner()).execute(rid)
run=svc.store.get(rid);assert run['state']=='waiting_approval',run.get('error')
print(json.dumps({'id':rid,'worktree':run['worktree']}))
`,
        f.config,
        repo,
        project.id,
      ],
      { encoding: "utf8" },
    ),
  );
  const target = join(pending.worktree, "tests/test_main.py");
  expect(readFileSync(target, "utf8")).toContain("add(0, 0)");
  await page.goto(`/runs/${pending.id}`);
  await expect(
    page.getByRole("heading", { name: "기존 테스트 변경 내역" }),
  ).toBeVisible();
  await expect(
    page.getByText(
      "기존 덧셈 검사를 보존하고 0 입력 경계값 검사를 추가합니다.",
      { exact: true },
    ),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "테스트 변경 승인", exact: true }),
  ).toHaveCount(0);
  await page.getByText("제품·테스트 전체 패치 보기", { exact: true }).click();
  await expect(page.locator(".test-change-patch")).toContainText(
    "+    assert add(0, 0) == 0",
  );
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
  await page.screenshot({
    path: "test-results/test-change-automatic.png",
    fullPage: true,
  });
  await expect(page.locator(".page-heading .status")).toHaveText("승인 대기");
  expect(readFileSync(join(repo, "tests/test_main.py"), "utf8")).not.toContain(
    "add(0, 0)",
  );
  execFileSync(join(dirname(f.python), "devorchestra"), [
    "--config",
    f.config,
    "approve",
    pending.id,
  ]);
  await expect(page.locator(".page-heading .status")).toHaveText("완료");
});
