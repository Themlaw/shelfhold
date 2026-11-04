import { expect, test } from '@playwright/test';

// ENF-PRIV-001: no third-party resource in the browser. Every request outside the
// instance origin is refused, and the test fails if one is even attempted.
test('test_spa_loads_no_third_party_resource', async ({ page, baseURL }) => {
  const origin = new URL(baseURL ?? '').origin;
  const attempted: string[] = [];

  await page.route('**/*', async (route) => {
    const url = route.request().url();
    if (url.startsWith('data:') || new URL(url).origin === origin) {
      await route.continue();
    } else {
      attempted.push(url);
      await route.abort();
    }
  });

  const cspViolations: string[] = [];
  page.on('console', (msg) => {
    if (/content security policy/i.test(msg.text())) cspViolations.push(msg.text());
  });

  await page.goto('/');
  await expect(page.getByRole('heading', { level: 1, name: 'Shelfhold — pré-alpha' })).toBeVisible();
  await expect(page.getByRole('status')).toHaveText(/Le serveur répond \(version /);
  await page.waitForLoadState('networkidle');

  expect(attempted, `third-party requests attempted: ${attempted.join(', ')}`).toEqual([]);
  expect(cspViolations).toEqual([]);
});

test('the SPA fallback serves client routes', async ({ page }) => {
  const response = await page.goto('/une/route/cliente');
  expect(response?.status()).toBe(200);
  await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
});
