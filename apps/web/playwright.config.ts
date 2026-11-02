import { defineConfig, devices } from "@playwright/test";
export default defineConfig({
  testDir: "./e2e",
  testMatch: "**/*.spec.ts",
  fullyParallel: false,
  workers: 1,
  timeout: 120000,
  expect: { timeout: 15000 },
  retries: 0,
  reporter: [["list"], ["html", { open: "never" }]],
  use: {
    baseURL: process.env.E2E_BUNDLED
      ? "http://127.0.0.1:18080"
      : "http://127.0.0.1:5173",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
        viewport: { width: 1440, height: 1000 },
      },
    },
  ],
  webServer: [
    {
      command: "../../.venv/bin/python e2e/backend.py",
      url: "http://127.0.0.1:18080/api/health",
      reuseExistingServer: false,
      timeout: 30000,
      env: { DEVORCHESTRA_TEST_SANDBOX: "1" },
      gracefulShutdown: { signal: "SIGTERM", timeout: 15000 },
    },
    ...(!process.env.E2E_BUNDLED
      ? [
          {
            command: "npm run dev",
            url: "http://127.0.0.1:5173",
            reuseExistingServer: false,
            timeout: 30000,
            env: { DEVORCHESTRA_API_PORT: "18080" },
            gracefulShutdown: { signal: "SIGTERM" as const, timeout: 5000 },
          },
        ]
      : []),
  ],
});
