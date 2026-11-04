import { defineConfig } from '@playwright/test';
export default defineConfig({
  testDir: 'tests', timeout: 60000, reporter: 'list',
  use: { baseURL: 'http://127.0.0.1:8765' },
  webServer: { command: 'uv run --locked --project ../../backend python serve.py build', url: 'http://127.0.0.1:8765/healthz', reuseExistingServer: false }
});
