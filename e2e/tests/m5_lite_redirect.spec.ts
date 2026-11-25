import { expect, test } from '../workshop/fixtures';

// M5 L17b (EF-LEC-010, ENF-COMP-002): a browser that cannot run a module script is sent to /lite
// by the page itself, by capability and never by User-Agent. Chromium runs modules, so the legacy
// browser is played by serving the page with the `nomodule` attribute taken off: the script then
// runs as it does in a browser that does not know `type="module"` (it ignores the attribute).

const LITE = /\/lite(\/|$)/;

// req: EF-LEC-010, ENF-COMP-002
test('nomodule browser lands on /lite', async ({ page }) => {
  await page.route('**/', async (route) => {
    if (route.request().resourceType() !== 'document') return route.continue();
    const response = await route.fetch();
    const html = (await response.text()).replace(/\snomodule(=""|="nomodule")?/g, '');
    await route.fulfill({ response, body: html });
  });
  await page.goto('/');
  await expect(page).toHaveURL(LITE);
});

// req: EF-LEC-010, ENF-COMP-002
test('a browser that runs modules stays on the SPA, whatever its User-Agent', async ({ browser }) => {
  for (const userAgent of ['Mozilla/5.0 (Linux; U) AppleWebKit/538.1 Kobo', 'Mozilla/5.0 Kindle/3.0']) {
    const context = await browser.newContext({ userAgent });
    const page = await context.newPage();
    await page.goto('/');
    await expect(page.getByRole('heading', { level: 1, name: 'Bonjour.' })).toBeVisible();
    expect(new URL(page.url()).pathname).toBe('/');
    await context.close();
  }
});

// req: EF-LEC-010, ENF-COMP-002
test('a browser without JavaScript lands on /lite', async ({ browser }) => {
  const context = await browser.newContext({ javaScriptEnabled: false });
  const page = await context.newPage();
  await page.goto('/');
  await expect(page).toHaveURL(LITE);
  await context.close();
});
