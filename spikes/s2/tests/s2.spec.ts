import { test, expect } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import { gzipSync } from 'node:zlib';

test('1+2 build servi par FastAPI, client généré branché', async ({ page }) => {
  const r = await page.goto('/');
  expect(r?.status()).toBe(200);
  await expect(page.getByTestId('health')).toHaveText('État du serveur : ok');
  const deep = await page.goto('/bibliotheque');
  expect(deep?.status()).toBe(200); // fallback SPA
});

test('4 i18n FR pluriels ICU', async ({ page }) => {
  await page.goto('/');
  const txt = (await page.locator('p').nth(1).innerText()).replace(/ | /g, ' ');
  console.log('i18n:', txt);
  expect(txt).toBe('Aucune œuvre / 1 œuvre / 2 000 œuvres');
});

test('3 liste virtualisée 2000, fluidité', async ({ page }) => {
  await page.goto('/bibliotheque');
  const sc = page.getByTestId('scroller');
  await expect(sc).toBeVisible();
  const rows = await page.locator('[data-row]').count();
  const res = await page.evaluate(async () => {
    const el = document.querySelector('[data-testid=scroller]') as HTMLElement;
    const total = el.scrollHeight - el.clientHeight;
    const frames: number[] = [];
    let longTasks = 0;
    new PerformanceObserver((l) => (longTasks += l.getEntries().length)).observe({ entryTypes: ['longtask'] });
    let last = performance.now();
    const t0 = last;
    await new Promise<void>((done) => {
      const step = () => {
        const now = performance.now();
        frames.push(now - last); last = now;
        el.scrollTop += total / 240; // balayage complet en ~240 images
        if (el.scrollTop >= total - 1) return done();
        requestAnimationFrame(step);
      };
      requestAnimationFrame(step);
    });
    frames.sort((a, b) => a - b);
    const maxDom = document.querySelectorAll('[data-row]').length;
    return { n: frames.length, p50: frames[Math.floor(frames.length * 0.5)], p95: frames[Math.floor(frames.length * 0.95)], max: frames.at(-1), longTasks, maxDom, ms: performance.now() - t0 };
  });
  console.log('virtual:', JSON.stringify({ rowsAtLoad: rows, ...res }));
  expect(res.maxDom).toBeLessThan(100);
  await expect(page.locator('[data-row]').last()).toContainText('Œuvre n° 2000');
});

for (const path of ['/', '/bibliotheque', '/reglages']) {
  test(`5 axe ${path}`, async ({ page }) => {
    await page.goto(path);
    await page.waitForLoadState('networkidle');
    const { violations } = await new AxeBuilder({ page }).analyze();
    console.log('axe', path, violations.map((v) => `${v.id}:${v.impact}`).join(',') || 'aucune');
    expect(violations.filter((v) => v.impact === 'serious' || v.impact === 'critical')).toEqual([]);
  });
}

test('6 taille du bundle initial', async ({ page }) => {
  const sizes: Record<string, { raw: number; gz: number }> = {};
  page.on('response', async (resp) => {
    const u = new URL(resp.url());
    if (!/\.(js|css|html)$|\/$/.test(u.pathname)) return;
    const body = await resp.body();
    sizes[u.pathname] = { raw: body.length, gz: gzipSync(body, { level: 9 }).length };
  });
  await page.goto('/');
  await page.waitForLoadState('networkidle');
  const vals = Object.values(sizes);
  const raw = vals.reduce((a, b) => a + b.raw, 0);
  const gz = vals.reduce((a, b) => a + b.gz, 0);
  console.log('bundle:', JSON.stringify({ files: vals.length, raw, gz }));
});
