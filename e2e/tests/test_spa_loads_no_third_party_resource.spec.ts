import { expect, test, type Page } from '@playwright/test';

// ENF-PRIV-001: no third-party resource in the browser. Every request outside the
// instance origin is refused, and the tests fail if one is even attempted.
async function refuseThirdParties(page: Page, origin: string): Promise<string[]> {
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
  return attempted;
}

test('test_spa_loads_no_third_party_resource', async ({ page, baseURL }) => {
  const attempted = await refuseThirdParties(page, new URL(baseURL ?? '').origin);
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

// The route guard must work on its own, without the CSP: serve a page with no CSP
// that reaches out to a third party, and check the interception records it.
test('the route guard catches third-party requests even without a CSP', async ({ page, baseURL }) => {
  const origin = new URL(baseURL ?? '').origin;
  const attempted = await refuseThirdParties(page, origin);
  await page.route(`${origin}/__no_csp__`, (route) =>
    route.fulfill({
      contentType: 'text/html',
      body: '<!doctype html><title>t</title><img src="https://third-party.invalid/pixel.png">'
    })
  );
  await page.goto('/__no_csp__');
  await page.evaluate(() => fetch('https://third-party.invalid/data.json').catch(() => null));

  expect(attempted).toContain('https://third-party.invalid/pixel.png');
  expect(attempted).toContain('https://third-party.invalid/data.json');
});

test('the SPA fallback serves client routes', async ({ page }) => {
  const response = await page.goto('/une/route/cliente');
  expect(response?.status()).toBe(200);
  await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
});

test('the index carries the CSP header with frame-ancestors', async ({ request }) => {
  const response = await request.get('/');
  const csp = response.headers()['content-security-policy'] ?? '';
  for (const directive of ["base-uri 'none'", "form-action 'self'", "object-src 'none'", "frame-ancestors 'none'"]) {
    expect(csp).toContain(directive);
  }
  expect(response.headers()['x-content-type-options']).toBe('nosniff');
});
