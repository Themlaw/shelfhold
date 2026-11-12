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


// The workshop harness (M3 L02): the SPA served by the real API on a synthetic base with three
// profiles, played on a desktop window and on 360x780. Opt-in (`npm run test:workshop`, CI job
// `frontend-e2e`) so that the image journeys above keep their own server and their own shards.
const WORKSHOP_PORT = 8792;
const WORKSHOP_DIR = '.tmp-workshop';
const workshopEnv = [
  `SHELFHOLD_DATA_DIR=${WORKSHOP_DIR}/data`,
  `SHELFHOLD_STORE_DIR=${WORKSHOP_DIR}/store`,
  `SHELFHOLD_FRONTEND_DIR=../frontend/build`,
  `SHELFHOLD_PORT=${WORKSHOP_PORT}`,
  `SHELFHOLD_PUBLIC_URL=http://localhost:${WORKSHOP_PORT}`,
  `SHELFHOLD_LOG_FORMAT=console`
].join(' ');
const workshopUrl = `http://localhost:${WORKSHOP_PORT}`;
const workshop = process.env.SHELFHOLD_E2E_SUITE === 'workshop';

const workshopConfig = defineConfig({
  testDir: 'workshop',
  testMatch: '**/*.spec.ts',
  reporter: 'list',
  fullyParallel: true,
  use: { baseURL: workshopUrl },
  projects: [
    { name: 'workshop-desktop', use: { viewport: { width: 1280, height: 800 } } },
    {
      name: 'workshop-mobile',
      use: { viewport: { width: 360, height: 780 }, isMobile: true, hasTouch: true }
    }
  ],
  webServer: {
    command: `rm -rf ${WORKSHOP_DIR} && mkdir -p ${WORKSHOP_DIR}/data ${WORKSHOP_DIR}/store && ${workshopEnv} ${shelfhold} migrate && (umask 077; echo ${ADMIN_PASSWORD} > ${WORKSHOP_DIR}/admin.pw) && ${workshopEnv} ${shelfhold} admin create --username admin --password-file ${WORKSHOP_DIR}/admin.pw && ${workshopEnv} uv run --locked --project ../backend python workshop/seed.py ${WORKSHOP_DIR}/data && ${workshopEnv} ${shelfhold} serve`,
    url: `http://127.0.0.1:${WORKSHOP_PORT}/healthz`,
    reuseExistingServer: false,
    timeout: 120_000
  }
});

export default workshop ? workshopConfig : defineConfig({
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
