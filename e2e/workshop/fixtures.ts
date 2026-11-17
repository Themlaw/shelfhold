import AxeBuilder from '@axe-core/playwright';
import { expect, test as base, type BrowserContext, type Page, type Route } from '@playwright/test';

// Sign-in by role on the synthetic base of `seed.py` (M3 L02): the administrator, a member and a
// child of 8. The passwords are invented for this base and mean nothing anywhere else.
export const ADMIN_PASSWORD = 'synthetic-admin-password-1';
export const PROFILE_PASSWORD = 'synthetic-profile-password-1';
export const ROLES = {
  admin: { username: 'admin', password: ADMIN_PASSWORD },
  member: { username: 'membre', password: PROFILE_PASSWORD },
  child: { username: 'enfant8', password: PROFILE_PASSWORD }
} as const;
export type Role = keyof typeof ROLES;

export async function signInAs(page: Page, role: Role): Promise<void> {
  const answer = await page.request.post('/api/v1/auth/login', { data: ROLES[role] });
  expect(answer.status(), `sign in as ${role}`).toBe(200);
}

/** The screens the cross-cutting specs (footer, CSP, theme) play on: one per kind of page. */
export const CORE_SCREENS = ['/', '/a-classer', '/a-ranger', '/compte/passkeys'];

const BLOCKING = ['serious', 'critical'];

/** axe on the page as it stands: any `serious` or `critical` violation fails the test. */
export async function expectNoBlockingViolations(page: Page): Promise<void> {
  const { violations } = await new AxeBuilder({ page })
    .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa'])
    .analyze();
  const blocking = violations.filter((v) => BLOCKING.includes(v.impact ?? ''));
  expect(
    blocking.map((v) => `${v.impact} ${v.id}: ${v.nodes.map((n) => n.target.join(' ')).join(' | ')}`),
    'axe: serious and critical violations'
  ).toEqual([]);
}

/** axe in the light scheme and then in the dark one: the tokens differ, so does the contrast. */
export async function expectScreenAccessible(page: Page): Promise<void> {
  for (const colorScheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme });
    await expectNoBlockingViolations(page);
  }
}

/** The page never scrolls sideways: at 360 px it reflows. */
export async function expectNoHorizontalScroll(page: Page): Promise<void> {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth
  );
  expect(overflow, 'horizontal overflow in px').toBeLessThanOrEqual(0);
}

/** Every control is at least 44 px by 44 px (a checkbox or a radio by its label). */
export async function expectTargets44(page: Page, path: string): Promise<void> {
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

/** Refuse and count every request whose origin is not the instance's (ENF-PRIV-001). */
export async function refuseOutsideBaseUrl(
  target: Page | BrowserContext,
  base: string
): Promise<string[]> {
  const origin = new URL(base || 'http://localhost').origin;
  const attempted: string[] = [];
  await target.route('**/*', async (route: Route) => {
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

export const test = base.extend({
  context: async ({ context, baseURL }, use) => {
    const outside = await refuseOutsideBaseUrl(context, baseURL ?? '');
    await use(context);
    expect(outside, 'requests outside baseURL').toEqual([]);
  }
});
export { expect };
