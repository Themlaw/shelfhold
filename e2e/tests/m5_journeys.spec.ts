import { expect, test, expectScreenAccessible, type Page } from '../workshop/fixtures';

// M5 L24: the journeys a browser plays on the IMAGE of the release (not on a local server), on the
// stack of `m5.yaml`. Like `test_m3_journey.spec.ts`, this file is not run by `e2e-front`
// (playwright.config.ts ignores it): the pytest wrapper `test_m5_journey.py` brings the stack up,
// seeds it (what an administrator types, what the screens send), passes what the spec needs in
// `E2E_M5` (identifiers and the synthetic credentials of the run, never a secret written here),
// runs this file with `playwright.image.config.ts`, then reads the outcome and the egress.
//
// Every context is fenced (`workshop/fixtures.ts`): a request whose origin is not the instance's is
// refused and counted, and that count is asserted empty at the end of each journey (ENF-PRIV-001).
// The names of the tests are those of 03 (`test_m5_journey_*`): the wrapper selects each by name.

interface World {
  admin: { username: string; password: string };
  book: { title: string; series_id: string; unit_id: string; carrier_id: string; marker: string };
  trapped: { title: string; unit_id: string; beacon: string };
  offline: { title: string; series_id: string; marker: string; work_id: string };
  lite: { identifier: string; secret: string; title: string; child: { identifier: string; secret: string } };
}

const world = (): World => JSON.parse(process.env.E2E_M5 ?? '{}') as World;

async function signIn(page: Page, who: { username: string; password: string }): Promise<void> {
  const answer = await page.request.post('/api/v1/auth/login', { data: who });
  expect(answer.status(), `sign in as ${who.username}`).toBe(200);
}

/** What the reader shows: its own page and every `blob:` document it opened for the book. */
const blobs = (page: Page) => page.frames().filter((f) => f.url().startsWith('blob:'));
async function bookText(page: Page): Promise<string> {
  const texts = await Promise.all(
    blobs(page).map((f) => f.locator('body').innerText({ timeout: 2_000 }).catch(() => ''))
  );
  return texts.join('\n');
}

/** Turn pages with the keyboard alone (ENF-A11Y-002) until `done` holds or `limit` turns are spent. */
async function readOn(page: Page, done: (text: string) => boolean, limit = 80): Promise<string> {
  let seen = '';
  for (let turn = 0; turn < limit; turn += 1) {
    seen += `\n${await bookText(page)}`;
    if (done(seen)) return seen;
    await page.keyboard.press('ArrowRight');
    await page.waitForTimeout(150);
  }
  return seen;
}

// req: EF-LEC-001, EF-LEC-003, EF-LEC-005, ENF-A11Y-002
test('test_m5_journey_read_excerpt_without_export: a kept book is read as an excerpt, then in full', async ({ page }) => {
  const { admin, book } = world();
  await signIn(page, admin);

  // the sheet of the series offers the kept copy ("Lire"): it is the administrator's, in full
  await page.goto(`/serie/${book.series_id}`);
  await expect(page.getByRole('link', { name: 'Lire', exact: true })).toBeVisible();

  // the ticket of the excerpt (bound to this session) opens the excerpt's chapters, and not the third
  const manifest = await (await page.request.get(`/api/reader/unit/${book.unit_id}/publication?scope=excerpt`)).json();
  const first: string = manifest.spine[0].href;
  expect((await page.request.get(first)).status()).toBe(200);
  expect((await page.request.get(first.replace('ch1.', 'ch3.'))).status()).toBe(404);

  // the excerpt: chosen on purpose, bounded by the server (a fifth of the book), badge and end
  await page.goto(`/lire/unit/${book.unit_id}?scope=excerpt`);
  await expect(page.getByText('Extrait', { exact: true })).toBeVisible();
  await expect.poll(() => bookText(page), { timeout: 30_000 }).toContain(`${book.marker} chapitre 1`);
  await expectScreenAccessible(page);
  const excerpt = await readOn(page, (text) => text.includes(`${book.marker} chapitre 3`), 40);
  expect(excerpt).toContain(`${book.marker} chapitre 2`);
  expect(excerpt, 'a chapter past the excerpt is never shown').not.toContain(`${book.marker} chapitre 3`);
  await expect(page.getByText('Fin de l’extrait')).toBeVisible();
  // an excerpt writes no position: nothing is stored for the book yet
  const none = await page.request.get(`/api/reader/content/${book.carrier_id}/position`);
  expect(JSON.stringify(await none.json())).not.toContain('epubcfi');

  // the whole book, as the kept copy it is: no badge, every chapter reachable by the keyboard
  await page.goto(`/lire/unit/${book.unit_id}`);
  await expect(page.getByText('Extrait', { exact: true })).toHaveCount(0);
  await expect.poll(() => bookText(page), { timeout: 30_000 }).toContain(`${book.marker} chapitre 1`);
  const full = await readOn(page, (text) => text.includes(`${book.marker} chapitre 10`));
  expect(full).toContain(`${book.marker} chapitre 10`);
  // and where the person is was written, on the kept copy
  await expect
    .poll(async () => JSON.stringify(await (await page.request.get(`/api/reader/content/${book.carrier_id}/position`)).json()))
    .toContain('epubcfi');
});

// req: EF-LEC-003, EF-LEC-005, ENF-SEC-004
test('test_m5_journey_epub_script_blocked: a booby-trapped book is shown and no script runs', async ({ page }) => {
  const { admin, trapped } = world();
  const calls: string[] = [];
  const errors: string[] = [];
  page.on('request', (r) => {
    if (r.url().includes(trapped.beacon)) calls.push(r.url());
  });
  page.on('pageerror', (e) => errors.push(String(e)));
  await signIn(page, admin);
  // what the reader is given is the neutralised derivative: no script, no event attribute, no link that runs
  const manifest = await (await page.request.get(`/api/reader/unit/${trapped.unit_id}/publication`)).json();
  const served = await (await page.request.get(manifest.spine[0].href)).text();
  expect(served).toContain('chapitre 1');
  for (const trap of ['<script', 'onerror', 'onload', 'onclick', 'javascript:']) expect(served).not.toContain(trap);
  await page.goto(`/lire/unit/${trapped.unit_id}`);
  await expect.poll(() => bookText(page), { timeout: 30_000 }).toContain('chapitre 1');
  // let any script that was going to run, run: an image that fails, a click, a link
  await page.waitForTimeout(1_500);
  for (const frame of blobs(page)) {
    await frame.locator('p', { hasText: 'Paragraphe piégé' }).click({ timeout: 2_000 }).catch(() => undefined);
    await frame.locator('a', { hasText: 'lien piégé' }).click({ timeout: 2_000 }).catch(() => undefined);
  }
  await page.waitForTimeout(500);
  expect(calls, 'a script of the book called back').toEqual([]);
  expect(errors, 'the page raised no error of its own').toEqual([]);
  // the document the reader opened holds no script at all, nor an event attribute
  for (const frame of blobs(page)) {
    expect(await frame.locator('script').count()).toBe(0);
    expect(await frame.locator('[onerror],[onload],[onclick]').count()).toBe(0);
  }
  await expectScreenAccessible(page);
});

// req: EF-LEC-010, ENF-COMP-002
test.describe('test_m5_journey_lite_without_javascript', () => {
  test.use({ javaScriptEnabled: false });
  test.setTimeout(120_000); // a hang must name its line, not run out the clock

  // req: EF-LEC-010, ENF-COMP-002
  test('/lite browses and downloads with JavaScript off, for the person the device is paired to', async ({ page }) => {
    const { lite } = world();
    await page.goto('/lite/sign-in');
    await page.getByLabel('Identifiant').fill(lite.identifier);
    await page.getByLabel('Secret').fill(lite.secret);
    await page.getByRole('button').click();
    await expect(page).toHaveURL(/\/lite\/?$/);

    await page.getByRole('link', { name: 'Toutes les œuvres' }).click();
    await page.getByRole('link', { name: lite.title }).first().click();
    const offer = page.getByRole('link', { name: /Télécharger/ }).first();
    await expect(offer).toBeVisible();
    const [download] = await Promise.all([page.waitForEvent('download'), offer.click()]);
    const path = await download.path();
    expect(path).toBeTruthy();
    expect(download.suggestedFilename()).toMatch(/\.(cbz|zip)$/i);
    expect(await page.content()).not.toContain('<script');
  });

  // req: EF-LEC-010, ENF-COMP-002
  test('the child of the household sees none of it', async ({ page }) => {
    const { lite } = world();
    await page.goto('/lite/sign-in');
    await page.getByLabel('Identifiant').fill(lite.child.identifier);
    await page.getByLabel('Secret').fill(lite.child.secret);
    await page.getByRole('button').click();
    await page.goto('/lite/works');
    await expect(page.getByRole('link', { name: lite.title })).toHaveCount(0);
  });
});

// req: EF-LEC-012
test('test_m5_journey_offline_eviction: offline read, purged, network back, copy evicted', async ({ page, context, browser, baseURL }) => {
  test.setTimeout(150_000); // a hang must name its line
  const { admin, offline } = world();
  await signIn(page, admin);
  // the worker in control of the page (a reload hands it over), as the installed app has it
  await page.goto('/');
  await page.evaluate(async () => {
    await navigator.serviceWorker.ready;
  });
  await page.reload();
  await expect.poll(() => page.evaluate(() => navigator.serviceWorker.controller !== null)).toBe(true);

  // 1. she keeps the book on this device, then finds it in her copies
  await page.goto(`/serie/${offline.series_id}`);
  await page.getByRole('button', { name: 'Lire hors ligne' }).click();
  await expect(page.getByText('Copie gardée sur cet appareil')).toBeVisible({ timeout: 60_000 });
  await page.goto('/hors-ligne');
  await expect(page.getByText(offline.title)).toBeVisible();
  // give the worker the time to keep the page of the reader and its files
  await page.waitForTimeout(3_000);

  // 2. the network is cut: the copy opens and turns pages
  await context.setOffline(true);
  await page.goto('/hors-ligne');
  await expect(page.getByText(offline.title)).toBeVisible();
  await page.getByRole('link', { name: 'Lire', exact: true }).click();
  await expect.poll(() => bookText(page), { timeout: 30_000 }).toContain(`${offline.marker} chapitre 1`);
  await expect(page.getByText('Copie hors ligne')).toBeVisible();
  await readOn(page, (text) => text.includes(`${offline.marker} chapitre 2`), 30);

  // 3. back on the list of copies (nothing leaves while the network is gone), the Work is purged from
  //    another browser (a fresh session: the purge is a reserved action): the copy is still on the device
  await page.goto('/hors-ligne');
  await expect(page.getByText(offline.title)).toBeVisible();
  const other = await browser.newContext({ baseURL: baseURL! });
  try {
    const answer = await other.request.post('/api/v1/auth/login', { data: admin });
    expect(answer.status()).toBe(200);
    const csrf = (await other.cookies()).find((c) => c.name.endsWith('csrf'))?.value ?? '';
    const purged = await other.request.post(`/api/v1/works/${offline.work_id}/purge`, {
      data: { typed: offline.work_id },
      headers: { 'X-CSRF-Token': csrf }
    });
    expect(purged.status(), await purged.text()).toBe(200);
  } finally {
    await other.close();
  }
  await expect(page.getByText(offline.title)).toBeVisible();

  // 4. the network is back: the feed says the Work left her view, the copy is evicted
  await context.setOffline(false);
  // (the list is read when the page opens: it is opened again until the sync of the reconnection has run)
  await expect
    .poll(
      async () => {
        await page.goto('/hors-ligne');
        return page.getByText('Aucune copie gardée sur cet appareil').waitFor({ timeout: 5_000 }).then(() => 1, () => 0);
      },
      { timeout: 60_000, intervals: [1_000, 2_000, 3_000] }
    )
    .toBe(1);
  await expect(page.getByText(offline.title)).toHaveCount(0);
  // and nothing of the book stays in the caches of the device
  const left = await page.evaluate(async () => {
    const urls: string[] = [];
    for (const name of await caches.keys()) for (const r of await (await caches.open(name)).keys()) urls.push(new URL(r.url).pathname);
    return urls.filter((p) => /\/(content|lire)\//.test(p) || p.includes('offline-copy'));
  });
  expect(left).toEqual([]);
});
