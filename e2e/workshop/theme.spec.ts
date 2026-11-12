import { expect, expectNoBlockingViolations, signInAs, test } from './fixtures';

// M3 L03: the theme mechanism, the CSP by hash and the reduced motion, played in a real browser on
// the SPA served by the real API.

const SCREENS = ['/', '/a-classer', '/a-ranger', '/compte/passkeys'];

/** Records every CSP violation and what the boot script did, before any script of the page runs. */
const RECORDER = `
  window.__violations = [];
  document.addEventListener('securitypolicyviolation', (e) => {
    window.__violations.push(e.effectiveDirective + ' ' + e.blockedURI);
  });
  window.__attributeSetWhen = [];
  new MutationObserver((records) => {
    for (const r of records) {
      window.__attributeSetWhen.push({
        name: r.attributeName,
        bodyExisted: document.body !== null,
        readyState: document.readyState
      });
    }
  }).observe(document, { subtree: true, attributes: true, attributeFilter: ['data-theme', 'data-color-scheme'] });
`;

// req: ENF-SEC-004
test('test_spa_screens_raise_no_csp_violation', async ({ page }) => {
  await signInAs(page, 'admin');
  await page.addInitScript(RECORDER);
  for (const path of SCREENS) {
    await page.goto(path);
    await expect(page.getByRole('contentinfo')).toBeVisible();
    expect(await page.evaluate(() => (window as never as { __violations: string[] }).__violations), path).toEqual([]);
  }
});

// req: ENF-SEC-004
test('test_spa_csp_header_is_by_hash_without_unsafe_inline', async ({ page }) => {
  for (const path of SCREENS) {
    const response = await page.request.get(path);
    const csp = response.headers()['content-security-policy'] ?? '';
    expect(csp, path).toMatch(/script-src [^;]*'sha256-/);
    expect(csp, path).not.toContain("'unsafe-inline'");
    expect(csp, path).not.toContain("'unsafe-eval'");
    expect(csp, path).not.toContain('nonce-');
    expect(csp, path).toMatch(/style-src 'self'(;|$)/);
    expect(csp, path).toContain("frame-ancestors 'none'");
  }
});

// req: ENF-SEC-004
test('test_inline_script_without_hash_is_refused_by_the_browser', async ({ page }) => {
  await signInAs(page, 'admin');
  await page.addInitScript(RECORDER);
  await page.goto('/a-classer');
  const ran = await page.evaluate(() => {
    const script = document.createElement('script');
    script.textContent = 'window.__injected = true';
    document.body.append(script);
    return (window as never as { __injected?: boolean }).__injected === true;
  });
  expect(ran).toBe(false);
  const violations = await page.evaluate(() => (window as never as { __violations: string[] }).__violations);
  expect(violations.some((v) => v.startsWith('script-src'))).toBe(true);
});

// req: ENF-SEC-004
test('test_inline_event_handler_and_javascript_url_are_refused', async ({ page }) => {
  await signInAs(page, 'admin');
  await page.goto('/a-classer');
  const ran = await page.evaluate(async () => {
    const win = window as never as { __handler?: boolean };
    const button = document.createElement('button');
    button.setAttribute('onclick', 'window.__handler = true');
    document.body.append(button);
    button.click();
    const link = document.createElement('a');
    link.href = 'javascript:window.__handler = true';
    document.body.append(link);
    link.click();
    await new Promise((r) => setTimeout(r, 50));
    return win.__handler === true;
  });
  expect(ran).toBe(false);
});

// req: ENF-SEC-004
test('test_inline_style_attribute_is_refused_by_the_browser', async ({ page }) => {
  await signInAs(page, 'admin');
  await page.goto('/a-classer');
  const color = await page.evaluate(() => {
    const probe = document.createElement('p');
    probe.textContent = 'probe';
    probe.setAttribute('style', 'color: rgb(1, 2, 3)');
    document.body.append(probe);
    return getComputedStyle(probe).color;
  });
  expect(color).not.toBe('rgb(1, 2, 3)');
  const sheet = await page.evaluate(() => {
    const style = document.createElement('style');
    style.textContent = 'body { outline: 7px solid rgb(1, 2, 3) }';
    document.head.append(style);
    return getComputedStyle(document.body).outlineWidth;
  });
  expect(sheet).not.toBe('7px');
});

// req: EF-UX-012
test('test_theme_attribute_is_set_before_the_first_render', async ({ page, context }) => {
  await signInAs(page, 'admin');
  const answer = await page.request.patch('/api/v1/me/preferences', {
    data: { color_scheme: 'dark' },
    headers: { 'X-CSRF-Token': (await context.cookies()).find((c) => c.name.endsWith('csrf'))?.value ?? '' }
  });
  expect(answer.status()).toBe(200);
  await page.goto('/a-classer'); // first visit: the layout remembers the preference
  await expect(page.locator('html')).toHaveAttribute('data-color-scheme', 'dark');
  await page.addInitScript(RECORDER);
  await page.reload(); // second visit: the boot script knows it before the body exists
  const sets = await page.evaluate(
    () => (window as never as { __attributeSetWhen: { name: string; bodyExisted: boolean }[] }).__attributeSetWhen
  );
  const first = sets.find((s) => s.name === 'data-color-scheme');
  expect(first, 'the attribute is set by the boot script').toBeDefined();
  expect(first!.bodyExisted).toBe(false);
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'bibliotheque');
});

// req: EF-UX-012
test('test_stored_preference_applies_in_dark_and_the_screen_stays_accessible', async ({ page, context }) => {
  test.setTimeout(120_000);
  await signInAs(page, 'admin');
  const csrf = (await context.cookies()).find((c) => c.name.endsWith('csrf'))?.value ?? '';
  for (const color_scheme of ['light', 'dark']) {
    await page.request.patch('/api/v1/me/preferences', { data: { color_scheme }, headers: { 'X-CSRF-Token': csrf } });
    await page.goto('/a-classer');
    await expect(page.locator('html')).toHaveAttribute('data-color-scheme', color_scheme);
    const background = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
    expect(background).toBe(color_scheme === 'dark' ? 'rgb(26, 23, 20)' : 'rgb(246, 241, 231)');
    if (color_scheme === 'dark') await expectNoBlockingViolations(page); // L02 plays both schemes
  }
});

// req: ENF-A11Y-003
test('test_reduced_motion', async ({ page }) => {
  await signInAs(page, 'admin');
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.goto('/a-classer');
  const reduced = await page.evaluate(async () => {
    const probe = document.createElement('div');
    probe.className = 'probe';
    probe.textContent = 'probe';
    document.body.append(probe);
    probe.classList.add('x');
    const style = getComputedStyle(probe);
    return {
      matches: matchMedia('(prefers-reduced-motion: reduce)').matches,
      transition: parseFloat(style.transitionDuration),
      animation: parseFloat(style.animationDuration),
      scroll: getComputedStyle(document.documentElement).scrollBehavior
    };
  });
  expect(reduced.matches).toBe(true);
  expect(reduced.transition).toBeLessThanOrEqual(0.001);
  expect(reduced.animation).toBeLessThanOrEqual(0.001);
  expect(reduced.scroll).toBe('auto');
});
