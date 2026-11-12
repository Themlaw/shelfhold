import AxeBuilder from '@axe-core/playwright';
import { expect, test as base, type Page } from '@playwright/test';

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

export const test = base;
export { expect };
