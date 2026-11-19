import type { Page } from '@playwright/test';
import { expect, test } from '../workshop/fixtures';

// M5 L16a: the installable shell, in a real browser (Chromium, Firefox, WebKit: `playwright.reader.config.ts`).
// The SPA and the reader are the pages the real server serves, with their real headers: what is
// proved here is what only a browser does. The worker registers under the hash policy of the SPA,
// keeps the shell, and never stands in for the reader (`/lire`, its own page and policy, ADR-0055).

const violations = (page: Page) => {
  const seen: string[] = [];
  page.on('console', (m) => /content security policy/i.test(m.text()) && seen.push(m.text()));
  return seen;
};

/** The worker, registered by the SPA and in control of the page (a reload hands the page to it). */
async function controlled(page: Page) {
  await page.goto('/');
  await page.evaluate(async () => {
    await navigator.serviceWorker.ready;
  });
  await page.reload();
  await expect.poll(() => page.evaluate(() => navigator.serviceWorker.controller !== null)).toBe(true);
}

const directive = (csp: string, name: string) =>
  csp.split(';').map((p) => p.trim().split(/\s+/)).find(([n]) => n === name)?.slice(1) ?? [];

// req: EF-LEC-012, ENF-SEC-004
test('service worker passes the hash CSP', async ({ page, request }) => {
  const seen = violations(page);
  const home = await page.goto('/');
  const csp = home!.headers()['content-security-policy']!;
  expect(csp).not.toMatch(/unsafe-inline|unsafe-eval/);
  expect(directive(csp, 'script-src')).toEqual(expect.arrayContaining(["'self'"]));

  const scope = await page.evaluate(async () => (await navigator.serviceWorker.ready).scope);
  expect(scope).toBe(new URL('/', page.url()).href);

  // the worker is a file of the origin with nothing dynamic in it
  const worker = await request.get('/service-worker.js');
  expect(worker.status()).toBe(200);
  expect(worker.headers()['content-type']).toMatch(/javascript/);
  expect(worker.headers()['cache-control']).toMatch(/no-cache/);
  const code = await worker.text();
  expect(code).not.toMatch(/\beval\s*\(|new Function|importScripts|https?:\/\//);

  // the manifest is linked, of the right type, and of the origin
  const href = await page.locator('link[rel="manifest"]').getAttribute('href');
  const manifest = await request.get(href!);
  expect(manifest.status()).toBe(200);
  expect(manifest.headers()['content-type']).toMatch(/application\/manifest\+json/);
  expect((await manifest.json()).start_url).toBe('/');

  await page.waitForLoadState('networkidle');
  expect(seen).toEqual([]);
});

// req: EF-LEC-012
test('test_pwa_offline_cache: a page of the SPA opens with the network cut', async ({ page, context, browserName }) => {
  // Firefox and WebKit refuse the navigation (NS_ERROR_OFFLINE) before the worker sees it when
  // Playwright cuts the network, so only Chromium can play this; the worker's own answer offline is
  // proved on all engines by `worker.test.ts`, and the shell kept by each engine by the next test.
  test.skip(browserName !== 'chromium', 'Playwright cannot cut the network under a service worker on this engine');
  await controlled(page);
  await context.setOffline(true);
  const response = await page.goto('/bibliotheque');
  expect(response?.status()).toBe(200);
  expect(response?.fromServiceWorker()).toBe(true);
  expect(await response!.text()).toContain('__sveltekit');
  await context.setOffline(false);
});

// req: EF-LEC-012
test('keeps no answer of the API and no byte of a book', async ({ page }) => {
  await controlled(page);
  await page.evaluate(() => fetch('/api/v1/source').catch(() => null));
  await page.evaluate(() => fetch('/content/TICKET/x.png').catch(() => null));
  const kept = await page.evaluate(async () => {
    const urls: string[] = [];
    for (const name of await caches.keys()) for (const r of await (await caches.open(name)).keys()) urls.push(new URL(r.url).pathname);
    return urls;
  });
  // the shell and a hashed asset are kept, in every engine
  expect(kept).toContain('/');
  expect(kept.some((p) => p.startsWith('/_app/immutable/'))).toBe(true);
  expect(kept.filter((p) => /^\/(api|content|lire|reader-entry|healthz)/.test(p))).toEqual([]);
});

// req: EF-LEC-012, ENF-SEC-004
test('test_offline_reader_entry_keeps_reader_csp', async ({ page, context }) => {
  await controlled(page);

  // in control of the origin, the worker lets the reader through: its page, its header, no shell
  const response = await page.goto('/lire/U1');
  expect(response?.fromServiceWorker()).toBe(false);
  const csp = response!.headers()['content-security-policy']!;
  expect(directive(csp, 'worker-src')).toEqual(["'none'"]);
  expect(directive(csp, 'manifest-src')).toEqual(["'none'"]);
  expect(directive(csp, 'script-src')).toContain("'strict-dynamic'");
  expect(await page.locator('link[rel="manifest"]').count()).toBe(0);
  expect(await page.locator('#reader').count()).toBe(1);

  // with no network, the reader entry is not replaced by the shell (it is cached by L16b, with its headers)
  await context.setOffline(true);
  const offline = await page.goto('/lire/U1').then(
    (r) => r,
    (error: Error) => error
  );
  if (offline instanceof Error) {
    // the right answer: the network error of the page, not a shell (L16b caches the reader with its
    // headers). The text is the engine's (ERR_INTERNET_DISCONNECTED, NS_ERROR_OFFLINE, "WebKit
    // encountered an internal error"): what is asserted is that the navigation failed.
    expect(offline.message).toMatch(/^page\.goto:/);
  } else {
    expect(offline?.fromServiceWorker()).toBe(false);
    expect(await offline!.text()).not.toContain('__sveltekit');
  }
  await context.setOffline(false);
});
