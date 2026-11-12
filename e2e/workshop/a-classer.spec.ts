import {
  expectNoHorizontalScroll,
  expectScreenAccessible,
  signInAs,
  test,
  expect
} from './fixtures';

// The first existing screen through the harness: "À classer" (EF-USR-015), on the desktop project
// and on 360x780, with axe blocking on serious and critical, in light and in dark.

// req: ENF-A11Y-001
test('test_a_classer_axe_no_blocking_violation', async ({ page }) => {
  await signInAs(page, 'admin');
  await page.goto('/a-classer');
  await expect(page.getByRole('heading', { level: 1, name: 'À classer' })).toBeVisible();
  await expect(page.getByRole('listitem').first()).toBeVisible();
  await expectScreenAccessible(page);
  await expectNoHorizontalScroll(page);
});

// req: ENF-A11Y-001
test('test_a_classer_batch_confirmation_axe_no_blocking_violation', async ({ page }) => {
  await signInAs(page, 'admin');
  await page.goto('/a-classer');
  const batch = page.getByRole('region', { name: /lot/i });
  await batch.getByRole('radio').first().check();
  await batch.getByRole('button').first().click();
  await expect(page.getByRole('textbox')).toBeVisible();
  await expectScreenAccessible(page);
  await expectNoHorizontalScroll(page);
});
