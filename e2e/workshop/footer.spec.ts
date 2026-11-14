import {
  CORE_SCREENS,
  expect,
  expectNoHorizontalScroll,
  signInAs,
  test,
  type Role
} from './fixtures';

// M3 L03: AGPL section 13. The link to the source is on every screen, for the administrator, a
// member, a child and someone who has not signed in.

const WHO: (Role | 'anonymous')[] = ['anonymous', 'admin', 'member', 'child'];

for (const who of WHO) {
  for (const path of CORE_SCREENS) {
    // req: ENF-LEG-012
    test(`test_ui_footer_has_source_link ${who} ${path}`, async ({ page, request }) => {
      if (who !== 'anonymous') await signInAs(page, who);
      const { source_url } = await (await request.get('/api/v1/source')).json();
      await page.goto(path);
      const footer = page.getByRole('contentinfo');
      await expect(footer).toBeVisible();
      const link = footer.getByRole('link', { name: 'Code source' });
      await expect(link).toBeVisible();
      await expect(link).toHaveAttribute('href', source_url);
    });
  }
}

// req: ENF-LEG-012
test('test_footer_source_link_is_reachable_without_scrolling_sideways_at_360', async ({ page }) => {
  await page.goto('/a-classer');
  const link = page.getByRole('contentinfo').getByRole('link', { name: 'Code source' });
  await link.scrollIntoViewIfNeeded();
  await expectNoHorizontalScroll(page);
  await expect(link).toBeInViewport();
});
