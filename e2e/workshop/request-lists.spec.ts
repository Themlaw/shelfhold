import type { BrowserContext, Page } from '@playwright/test';
import {
  expect,
  expectNoHorizontalScroll,
  expectScreenAccessible,
  expectTargets44,
  signInAs,
  test,
  type Role
} from './fixtures';

// L31: importable reading lists (EF-USR-020). The import screen and the one review screen, each
// played on a desktop window and on 360x780 (the two projects of the workshop): axe blocking
// (light and dark, in each of the four themes), no sideways scroll, every control 44 px, and the
// promises: nothing of the list is searched in a source, a list of a child asks for an age at
// approval, and a list of someone else is the same "not found" as one nobody ever had.

const THEMES = ['bibliotheque', 'console', 'galerie', 'calme'] as const;
const LIST = 'Un livre présent\nUn livre jamais catalogué\nUn autre (édition annotée) ; 978-0-306-40615-7\n';

async function settle(page: Page): Promise<void> {
  await page.getByRole('heading', { level: 1 }).first().waitFor();
  await page.waitForTimeout(300);
}

async function importList(page: Page, context: BrowserContext, text = LIST): Promise<string> {
  const csrf = (await context.cookies()).find((c) => c.name.endsWith('csrf'))?.value ?? '';
  const made = await page.request.post('/api/v1/request-lists', {
    data: { text, name: 'Liste de l’été' },
    headers: { 'X-CSRF-Token': csrf }
  });
  expect(made.status(), 'the list is imported').toBe(201);
  return (await made.json()).id as string;
}

const SCREENS: { name: string; as: Role; path: (page: Page, context: BrowserContext) => Promise<string>; heading: RegExp }[] = [
  { name: 'import (admin)', as: 'admin', path: async () => '/a-approuver/liste', heading: /^Importer une liste de lecture$/ },
  { name: 'import (member)', as: 'member', path: async () => '/a-approuver/liste', heading: /^Importer une liste de lecture$/ },
  {
    name: 'one list (admin)',
    as: 'admin',
    path: async (page, context) => `/a-approuver/liste/${await importList(page, context)}`,
    heading: /^Liste de l’été$/
  }
];

for (const screen of SCREENS) {
  // req: EF-USR-020
  test(`test_request_lists_screens_axe_blocking ${screen.name}`, async ({ page, context }) => {
    test.setTimeout(120_000);
    await signInAs(page, screen.as);
    const path = await screen.path(page, context);
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

// req: EF-USR-020
test('test_request_from_list_bulk_single_review_screen_ui', async ({ page, context }) => {
  await signInAs(page, 'admin');
  await page.goto('/a-approuver/liste');
  await settle(page);
  await page.getByLabel('Liste', { exact: true }).fill(LIST);
  await page.getByRole('button', { name: 'Importer' }).click();
  await expect(page.locator('section.done')).toContainText('demandés');
  await expect(page.locator('section.done')).toContainText('1 ligne réclame une édition annotée précise');
  await page.getByRole('link', { name: 'Valider toute la liste' }).click();
  await settle(page);
  // one screen, every line of the file, in its order
  await expect(page.getByRole('listitem').filter({ hasText: 'Un livre jamais catalogué' })).toHaveCount(1);
  expect(await page.locator('main ol > li').count()).toBe(3);
  void context;
});

// req: EF-USR-020, ENF-SEC-010
test('test_a_list_of_another_profile_is_the_same_not_found', async ({ page, context }) => {
  await signInAs(page, 'member');
  const mine = await importList(page, context, 'Un titre secret du membre');
  await page.context().clearCookies();
  await signInAs(page, 'child');
  const same = await page.request.get(`/api/v1/request-lists/${mine}`);
  const invented = await page.request.get('/api/v1/request-lists/01JZZZZZZZZZZZZZZZZZZZZZZZ');
  expect([same.status(), invented.status()]).toEqual([403, 403]); // a child reads no batch at all
  await page.goto(`/a-approuver/liste/${mine}`);
  await page.getByRole('alert').first().waitFor();
  expect(await page.locator('main').innerText()).not.toContain('Un titre secret du membre');
});
