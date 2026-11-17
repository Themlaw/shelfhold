import type { Page } from '@playwright/test';
import {
  expect,
  expectNoHorizontalScroll,
  expectScreenAccessible,
  expectTargets44,
  signInAs,
  test,
  type Role
} from './fixtures';

// L33b: collections and lists (EF-BIB-016) and the state of the sync per member (EF-EXP-026), each
// screen played on a desktop window and on 360x780 (the two projects of the workshop): axe blocking
// (light and dark, in each of the four themes), no sideways scroll, every control 44 px, and the
// promises: a child reads a list without a name and nothing of what is hidden, and a member reads
// only their own line of the sync, never a key.

const THEMES = ['bibliotheque', 'console', 'galerie', 'calme'] as const;

async function settle(page: Page): Promise<void> {
  await page.getByRole('heading', { level: 1 }).first().waitFor();
  await page.waitForTimeout(300);
}

async function firstCollectionId(page: Page): Promise<string> {
  const listed = await page.request.get('/api/v1/collections');
  expect(listed.status()).toBe(200);
  const items = (await listed.json()).items as { id: string }[];
  expect(items.length, 'the seed makes a list for the member').toBeGreaterThan(0);
  return items[0].id;
}

const SCREENS: { name: string; as: Role; path: (page: Page) => Promise<string>; heading: RegExp }[] = [
  { name: 'collections', as: 'member', path: async () => '/collections', heading: /^Collections et listes$/ },
  { name: 'collections (admin)', as: 'admin', path: async () => '/collections', heading: /^Collections et listes$/ },
  {
    name: 'one list',
    as: 'member',
    path: async (page) => `/collections/${await firstCollectionId(page)}`,
    heading: /./
  },
  { name: 'sync status', as: 'member', path: async () => '/reglages/compte-kavita', heading: /^Mon compte Kavita$/ }
];

for (const screen of SCREENS) {
  // req: EF-BIB-016, EF-EXP-026
  test(`test_collections_screens_axe_blocking ${screen.name}`, async ({ page }) => {
    test.setTimeout(120_000);
    await signInAs(page, screen.as);
    const path = await screen.path(page);
    await page.goto(path);
    await settle(page);
    await expect(page.getByRole('heading', { level: 1 }), `${path}: one h1`).toHaveCount(1);
    await expect(page.getByRole('heading', { level: 1 })).toHaveText(screen.heading);
    for (const theme of THEMES) {
      await page.evaluate((name) => {
        document.documentElement.dataset.theme = name;
      }, theme);
      await expectScreenAccessible(page);
    }
    await expectNoHorizontalScroll(page);
    await expectTargets44(page, path);
  });
}

// req: EF-EXP-026
test('test_sync_status_shows_only_own_member_to_non_admin', async ({ page }) => {
  await signInAs(page, 'member');
  await page.goto('/reglages/compte-kavita');
  await settle(page);
  const status = page.locator('section', { has: page.getByRole('heading', { name: 'Synchronisation de la lecture', level: 2 }) });
  await expect(status.getByRole('heading', { level: 4, name: /Adultes/ })).toBeVisible();
  await expect(status).toContainText('membre');
  await expect(status).not.toContainText('admin');
  // the door is the same by the API: no parameter widens it
  const admin = await page.request.get('/api/v1/sync-status?user_id=admin&all=true');
  const names = new Set(((await admin.json()).items as { username: string }[]).map((r) => r.username));
  expect([...names]).toEqual(['membre']);
});

// req: EF-EXP-026
test('test_sync_status_admin_reads_every_member_and_no_key_is_in_the_page', async ({ page }) => {
  await signInAs(page, 'admin');
  await page.goto('/reglages/compte-kavita');
  await settle(page);
  const status = page.locator('section', { has: page.getByRole('heading', { name: 'Synchronisation de la lecture', level: 2 }) });
  await expect(status).toContainText('membre');
  await expect(status).toContainText('admin');
  await expect(status).toContainText('Pas de jeton lié'); // members who have not linked a key are named
  const everything = await page.evaluate(
    () => document.documentElement.outerHTML + JSON.stringify({ ...localStorage }) + JSON.stringify({ ...sessionStorage })
  );
  expect(everything).not.toMatch(/token_fingerprint|api[-_]?key/i);
});

// req: EF-BIB-016
test('test_child_reads_a_list_without_a_name_and_nothing_is_guessed', async ({ page }) => {
  await signInAs(page, 'child');
  // the server withholds the name of a household collection that holds a title above the age of
  // the child; the screen draws "Liste sans nom" and the count it was given
  await page.route('**/api/v1/collections', async (route) => {
    await route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({
        items: [{ id: '01JCOLL000000000000000000A', kind: 'collection', name: null, scope: 'shared', mine: false, count: 2, cover_hash: null, created_at: '2026-11-10T10:00:00Z' }]
      })
    });
  });
  await page.goto('/collections');
  await settle(page);
  await expect(page.getByRole('link', { name: 'Liste sans nom' })).toBeVisible();
  const text = (await page.locator('main').innerText()).toLowerCase();
  expect(text).not.toMatch(/null|undefined|masqu|cach/);
  await expect(page.getByLabel('Portée')).toHaveCount(0); // a child never makes a household collection
});

// req: EF-BIB-016
test('test_a_list_of_another_profile_is_the_same_not_found', async ({ page }) => {
  await signInAs(page, 'child');
  const invented = await page.request.get('/api/v1/collections/01JZZZZZZZZZZZZZZZZZZZZZZZ');
  expect(invented.status()).toBe(404);
  await page.goto('/collections/01JZZZZZZZZZZZZZZZZZZZZZZZ');
  await page.getByRole('alert').first().waitFor();
  await expect(page.getByRole('alert')).toBeVisible();
});
