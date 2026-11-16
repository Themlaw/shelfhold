import type { Page } from '@playwright/test';
import {
  expect,
  expectNoHorizontalScroll,
  expectScreenAccessible,
  signInAs,
  test,
  type Role
} from './fixtures';

// L33a: the three screens of the destinations (EF-EXP-002, EF-USR-012), each played on a desktop
// window and on 360x780 (the two projects of the workshop): axe blocking (light and dark, in each
// of the four themes), no sideways scroll, every control 44 px, and the two promises of the
// screens: a family shelf that is refused a destination says why, and the Kavita key of a member
// is never shown again.

const THEMES = ['bibliotheque', 'console', 'galerie', 'calme'] as const;
const SCREENS: { path: string; as: Role; heading: string }[] = [
  { path: '/reglages/etageres', as: 'admin', heading: 'Étagères' },
  { path: '/reglages/destinations', as: 'admin', heading: 'Destinations' },
  { path: '/reglages/compte-kavita', as: 'member', heading: 'Mon compte Kavita' }
];
// drawn at each run: no literal that a secret scanner would flag
const KEY = ['kv', ...Array.from({ length: 3 }, () => Math.random().toString(36).slice(2, 10))].join('');

async function settle(page: Page): Promise<void> {
  await page.getByRole('heading', { level: 1 }).first().waitFor();
  await page.waitForTimeout(300);
}

async function expectTargets44(page: Page, path: string): Promise<void> {
  const tooSmall = await page.evaluate(() => {
    const small: string[] = [];
    for (const el of document.querySelectorAll<HTMLElement>(
      'button, a[href], input:not([type="hidden"]), select, textarea, summary'
    )) {
      if (el.getClientRects().length === 0 || getComputedStyle(el).visibility === 'hidden') continue;
      const isChoice = el instanceof HTMLInputElement && (el.type === 'checkbox' || el.type === 'radio');
      const rect = (isChoice ? (el.closest('label') ?? el) : el).getBoundingClientRect();
      if (rect.width < 43.5 || rect.height < 43.5) {
        small.push(`${el.tagName} ${el.textContent?.trim().slice(0, 20) ?? ''} ${rect.width.toFixed(0)}x${rect.height.toFixed(0)}`);
      }
    }
    return small;
  });
  expect(tooSmall, `${path}: targets under 44x44 px`).toEqual([]);
}

for (const screen of SCREENS) {
  // req: EF-EXP-002
  test(`test_destination_screens_axe_blocking ${screen.path}`, async ({ page }) => {
    test.setTimeout(120_000);
    await signInAs(page, screen.as);
    await page.goto(screen.path);
    await settle(page);
    await expect(page.getByRole('heading', { level: 1 })).toHaveText(screen.heading);
    await expect(page.getByRole('heading', { level: 1 }), `${screen.path}: one h1`).toHaveCount(1);
    for (const theme of THEMES) {
      await page.evaluate((name) => {
        document.documentElement.dataset.theme = name;
      }, theme);
      await expectScreenAccessible(page);
    }
    await expectNoHorizontalScroll(page);
    await expectTargets44(page, screen.path);
  });
}

// req: EF-EXP-047
test('test_family_shelf_destination_refusal_shows_reason', async ({ page }) => {
  await signInAs(page, 'admin');
  await page.goto('/reglages/etageres');
  await settle(page);
  const family = page.locator('section', {
    has: page.getByRole('heading', { name: 'Destinations de « Famille »', level: 2 })
  });
  await family.getByLabel('Serveur').selectOption({ label: 'Dossier partagé' }); // a folder keeps nothing apart
  await family.getByRole('button', { name: 'Lier la destination' }).click();
  const refusal = family.getByRole('alert');
  await expect(refusal).toContainText('shelf.destination_not_partitioned'); // the stable code
  await expect(refusal).toContainText('cloisonn'); // the title, the cause and the action of the server
  await expect(family.getByRole('listitem')).toHaveCount(0); // nothing was linked
});

// req: EF-USR-012
test('test_member_kavita_key_never_redisplayed', async ({ page }) => {
  await signInAs(page, 'member');
  let received: string | null = null;
  const sent: string[] = [];
  // the simulated Kavita of this bench is not reachable: the verdict of the server on a key is
  // proved by the backend tests (`test_destination_accounts.py`); what is proved here is what the
  // screen does with the key, whatever the verdict
  await page.route('**/api/v1/profiles/*/destination-accounts**', async (route) => {
    const request = route.request();
    if (request.method() === 'PUT') {
      received = request.postData();
      const destination = request.url().split('/').pop() as string;
      const body = JSON.stringify({
        id: '01JACCT000000000000000000A', user_id: '01JUSER000000000000000000M',
        shelf_destination_id: destination, remote_user: 'mum.kv', token_fingerprint: 'a1b2c3d4',
        verified_at: '2026-11-16T10:00:00Z', revoked_at: null, warnings: []
      });
      sent.push(body);
      await route.fulfill({ status: 200, contentType: 'application/json', body });
    } else {
      await route.continue();
    }
  });
  page.on('response', async (response) => {
    if (response.url().includes('/api/')) sent.push(await response.text().catch(() => ''));
  });
  await page.goto('/reglages/compte-kavita');
  await settle(page);
  const card = page.locator('section', { has: page.getByRole('heading', { level: 2 }) }).first();
  await card.getByLabel('Nom de votre compte Kavita').fill('mum.kv');
  await card.getByLabel('Clé de votre compte Kavita').fill(KEY);
  await card.getByRole('button', { name: 'Lier la clé' }).click();
  await expect(card.getByLabel('Clé de votre compte Kavita')).toHaveValue('');
  expect(received, 'the key went in the body of the request').toContain(KEY);
  await page.reload();
  await settle(page);
  const everything = await page.evaluate(
    () =>
      document.documentElement.outerHTML +
      JSON.stringify({ ...localStorage }) +
      JSON.stringify({ ...sessionStorage })
  );
  expect(everything).not.toContain(KEY);
  expect(sent.join('\n')).not.toContain(KEY);
  expect(await page.context().cookies()).not.toEqual(expect.arrayContaining([expect.objectContaining({ value: KEY })]));
});
