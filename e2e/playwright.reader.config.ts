import { defineConfig, devices } from '@playwright/test';
import base from './playwright.config';

// M5 L12: the EPUB reader, in the three engines of ADR-0055 (the inheritance of the policy by a
// `blob:` is a behaviour of the browser: a failure on one of them blocks the merge). L16a: the
// installable shell is played there too (a service worker and its policy are the browser's).
export default defineConfig({
  ...base,
  testDir: 'tests',
  testMatch: ['m5_reader_epub.spec.ts', 'm5_pwa.spec.ts'],
  testIgnore: [],
  fullyParallel: false,
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
    { name: 'firefox', use: { ...devices['Desktop Firefox'] } },
    { name: 'webkit', use: { ...devices['Desktop Safari'] } }
  ]
});
