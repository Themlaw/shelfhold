import { defineConfig } from '@playwright/test';

const PORT = 8791;
const env = [
  `SHELFHOLD_DATA_DIR=.tmp/data`,
  `SHELFHOLD_STORE_DIR=.tmp/store`,
  `SHELFHOLD_FRONTEND_DIR=../frontend/build`,
  `SHELFHOLD_PORT=${PORT}`,
  `SHELFHOLD_LOG_FORMAT=console`
].join(' ');
const shelfhold = 'uv run --locked --project ../backend shelfhold';

export default defineConfig({
  testDir: 'tests',
  reporter: 'list',
  use: { baseURL: `http://127.0.0.1:${PORT}` },
  webServer: {
    command: `rm -rf .tmp && mkdir -p .tmp/data .tmp/store && ${env} ${shelfhold} migrate && ${env} ${shelfhold} serve`,
    url: `http://127.0.0.1:${PORT}/healthz`,
    reuseExistingServer: false,
    timeout: 60_000
  }
});
