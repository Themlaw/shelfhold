import { expect, ROLES, signInAs, test, type Role } from './fixtures';

// req-file: ENF-A11Y-001

// The harness itself: the synthetic base holds the three profiles, and nothing leaves the host.
for (const role of Object.keys(ROLES) as Role[]) {
  test(`test_workshop_harness_signs_in_as_${role}`, async ({ page }) => {
    await signInAs(page, role);
    const me = await page.request.get('/api/v1/auth/session');
    expect([200, 404]).toContain(me.status());
  });
}

test('test_workshop_harness_refuses_a_wrong_password', async ({ page }) => {
  const answer = await page.request.post('/api/v1/auth/login', {
    data: { username: 'enfant8', password: 'not-the-password' }
  });
  expect(answer.status()).toBe(401);
});

test('test_workshop_harness_reaches_no_third_party', async ({ page, baseURL }) => {
  const origin = new URL(baseURL ?? '').origin;
  const foreign: string[] = [];
  page.on('request', (r) => {
    const url = r.url();
    if (!url.startsWith('data:') && new URL(url).origin !== origin) foreign.push(url);
  });
  await page.goto('/');
  await page.waitForLoadState('networkidle');
  expect(foreign).toEqual([]);
});
