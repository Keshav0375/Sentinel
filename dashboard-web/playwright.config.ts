import { defineConfig, devices } from '@playwright/test';

const CI = !!process.env.CI;

export default defineConfig({
  testDir: 'e2e',
  fullyParallel: true,
  forbidOnly: CI,
  retries: CI ? 1 : 0,
  reporter: CI ? 'github' : 'list',
  use: {
    baseURL: 'http://localhost:4173/Sentinel/',
    trace: 'retain-on-failure',
  },
  webServer: {
    // The production build, served under the Pages base path. Snapshot files are routed
    // per test (e2e/smoke.spec.ts), so the build itself ships none.
    command: 'corepack pnpm run build && corepack pnpm run preview',
    url: 'http://localhost:4173/Sentinel/',
    reuseExistingServer: !CI,
    timeout: 180_000,
  },
  projects: [
    { name: 'light', use: { ...devices['Desktop Chrome'], colorScheme: 'light' } },
    { name: 'dark', use: { ...devices['Desktop Chrome'], colorScheme: 'dark' } },
    {
      name: 'mobile-360',
      use: { ...devices['Desktop Chrome'], viewport: { width: 360, height: 740 }, colorScheme: 'light', hasTouch: true },
    },
  ],
});
