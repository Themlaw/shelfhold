import { expect, refuseOutsideBaseUrl, test, type Page } from '../workshop/fixtures';

// ENF-PRIV-001: no third-party resource in the browser. Every request outside the
// instance origin is refused, and the tests fail if one is even attempted.
// The fence is installed automatically on every context by the extended test
// (see workshop/fixtures.ts); its counter is asserted to be empty at teardown.

// req: ENF-PRIV-001
test('test_spa_loads_no_third_party_resource', async ({ page }) => {
  const cspViolations: string[] = [];
  page.on('console', (msg) => {
    if (/content security policy/i.test(msg.text())) cspViolations.push(msg.text());
  });

  await page.goto('/');
  await expect(page.getByRole('heading', { level: 1, name: 'Shelfhold — pré-alpha' })).toBeVisible();
  await expect(page.getByRole('status')).toHaveText('Le serveur répond.');
  await page.waitForLoadState('networkidle');

  expect(cspViolations).toEqual([]);
});

// The two M1b screens, without a session: the API answers 401, the page says so, and not one
// request leaves the instance origin nor breaks the CSP (no inline script, no third-party font).
for (const [path, heading] of [
  ['/a-classer', 'À classer'],
  ['/a-ranger', 'À ranger']
] as const) {
  // req: ENF-PRIV-001, EF-USR-015, EF-EXP-046
  test(`test_spa_loads_no_third_party_resource ${path}`, async ({ page }) => {
    const cspViolations: string[] = [];
    page.on('console', (msg) => {
      if (/content security policy/i.test(msg.text())) cspViolations.push(msg.text());
    });

    await page.goto(path);
    await expect(page.getByRole('heading', { level: 1, name: heading })).toBeVisible();
    await expect(page.getByRole('alert')).toContainText('Connexion requise');
    await page.waitForLoadState('networkidle');

    expect(cspViolations).toEqual([]);
  });
}

test('the SPA fallback serves client routes', async ({ page }) => {
  const response = await page.goto('/une/route/cliente');
  expect(response?.status()).toBe(200);
  await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
});

// req: ENF-SEC-004, ENF-PRIV-001
test('the index carries the CSP header with frame-ancestors', async ({ request }) => {
  const response = await request.get('/');
  const csp = response.headers()['content-security-policy'] ?? '';
  for (const directive of ["base-uri 'none'", "form-action 'self'", "object-src 'none'", "frame-ancestors 'none'"]) {
    expect(csp).toContain(directive);
  }
  expect(response.headers()['x-content-type-options']).toBe('nosniff');
});

// Proof that the fence counts: an image whose origin is not the instance's is refused and counted.
// A separate context is fenced explicitly, so the automatic fence of the extended test is not doubled.
// req: ENF-PRIV-001
test('test_spa_requests_outside_base_url_are_refused_and_counted', async ({ browser, baseURL }) => {
  const context = await browser.newContext({ baseURL });
  const page = await context.newPage();
  const outside = await refuseOutsideBaseUrl(context, baseURL ?? '');
  await page.goto('/');
  await page.evaluate(() => {
    const img = document.createElement('img');
    img.src = 'https://example.com/shelfhold-outside.png';
    document.body.append(img);
  });
  await expect.poll(() => outside.length, { timeout: 5000 }).toBe(1);
  expect(outside[0]).toContain('example.com');
  await context.close();
});