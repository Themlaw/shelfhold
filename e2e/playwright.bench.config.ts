import { defineConfig } from '@playwright/test';

// The reader bench (M5 L15, ENF-PERF-006): the SPA served by the real API on an empty synthetic
// base, the book answered by routes like tests/m5_reader_images.spec.ts — what a browser must
// draw is the turn of a page, the server side of the reader is proved elsewhere. A project
// apart: `npm run test:bench` only, never in the usual e2e (playwright.config.ts reads
// tests/), never in the nightly — the budget is measured before a release
// (docs/governance/benchmarks.md).
const PORT = 8793;
const DIR = '.tmp-bench';
const env = [
  `SHELFHOLD_DATA_DIR=${DIR}/data`,
  `SHELFHOLD_STORE_DIR=${DIR}/store`,
  `SHELFHOLD_FRONTEND_DIR=../frontend/build`,
  `SHELFHOLD_PORT=${PORT}`,
  `SHELFHOLD_PUBLIC_URL=http://localhost:${PORT}`,
  `SHELFHOLD_LOG_FORMAT=console`,
  // the bench signs in from the same address as every other local suite: the public rate limit
  // (ENF-SEC-008) is raised explicitly here, and nowhere else
  `SHELFHOLD_HTTP_PUBLIC_RATE_PER_MINUTE=100000`
].join(' ');
const shelfhold = 'uv run --locked --project ../backend shelfhold';
const ADMIN_PASSWORD = 'synthetic-admin-password-1';

export default defineConfig({
  testDir: 'bench',
  testMatch: '**/*.spec.ts',
  reporter: 'list',
  // one browser, one turn at a time: the median of the runs measures the reader, not neighbours
  workers: 1,
  fullyParallel: false,
  retries: 0,
  timeout: 5 * 60_000,
  use: { baseURL: `http://127.0.0.1:${PORT}`, locale: 'fr-FR' },
  webServer: {
    command: `rm -rf ${DIR} && mkdir -p ${DIR}/data ${DIR}/store && ${env} ${shelfhold} migrate && (umask 077; echo ${ADMIN_PASSWORD} > ${DIR}/admin.pw) && ${env} ${shelfhold} admin create --username admin --password-file ${DIR}/admin.pw && ${env} ${shelfhold} serve`,
    url: `http://127.0.0.1:${PORT}/healthz`,
    reuseExistingServer: false,
    timeout: 60_000
  }
});
