import { defineConfig } from '@playwright/test';

// The journeys of the image (M3 L57): a browser on the runner, the stack of `m3.yaml` behind the
// published port. No web server is started here: `test_m3_journey.py` brings the stack up and
// sets E2E_BASE_URL and E2E_COMPOSE. Never part of `e2e-front` (see playwright.config.ts).
export default defineConfig({
  testDir: 'tests',
  testMatch: 'test_m3_journey.spec.ts',
  reporter: 'list',
  workers: 1,
  fullyParallel: false,
  retries: 0,
  timeout: 15 * 60_000,
  expect: { timeout: 20_000 },
  use: {
    baseURL: process.env.E2E_BASE_URL ?? 'http://127.0.0.1:18080',
    locale: 'fr-FR',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure'
  }
});
