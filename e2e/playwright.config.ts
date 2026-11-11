import { defineConfig } from '@playwright/test';

const PORT = 8791;
const env = [
  `SHELFHOLD_DATA_DIR=.tmp/data`,
  `SHELFHOLD_STORE_DIR=.tmp/store`,
  `SHELFHOLD_FRONTEND_DIR=../frontend/build`,
  `SHELFHOLD_PORT=${PORT}`,
  // passkeys need a secure context: the RP ID is the host of this URL, so `localhost`
  `SHELFHOLD_PUBLIC_URL=http://localhost:${PORT}`,
  `SHELFHOLD_LOG_FORMAT=console`
].join(' ');
export const ADMIN_PASSWORD = 'synthetic-admin-password-1';
const shelfhold = 'uv run --locked --project ../backend shelfhold';

export default defineConfig({
  testDir: 'tests',
  reporter: 'list',
  use: { baseURL: `http://127.0.0.1:${PORT}` },
  webServer: {
    command: `rm -rf .tmp && mkdir -p .tmp/data .tmp/store && ${env} ${shelfhold} migrate && (umask 077; echo ${ADMIN_PASSWORD} > .tmp/admin.pw) && ${env} ${shelfhold} admin create --username admin --password-file .tmp/admin.pw && ${env} ${shelfhold} serve`,
    url: `http://127.0.0.1:${PORT}/healthz`,
    reuseExistingServer: false,
    timeout: 60_000
  }
});
