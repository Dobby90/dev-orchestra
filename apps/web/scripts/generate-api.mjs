import { execFileSync } from "node:child_process";
import { readFileSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
const cli = join(
  dirname(require.resolve("openapi-typescript/package.json")),
  "bin/cli.js",
);
const target = "src/api/generated.ts";
const temp = mkdtempSync(join(tmpdir(), "devorchestra-types-"));
try {
  const output = process.argv.includes("--check")
    ? join(temp, "api.ts")
    : target;
  execFileSync(process.execPath, [cli, "openapi.json", "-o", output], {
    stdio: "inherit",
  });
  if (
    output !== target &&
    readFileSync(output, "utf8") !== readFileSync(target, "utf8")
  ) {
    throw new Error(
      "API 타입이 변경되었습니다. npm run generate:api를 실행하세요.",
    );
  }
} finally {
  rmSync(temp, { recursive: true, force: true });
}
