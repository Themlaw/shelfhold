import {
  expect,
  expectNoHorizontalScroll,
  expectScreenAccessible,
  signInAs,
  test
} from './fixtures';

// The home with its « Reprendre la lecture » section filled (EF-LEC-008, ENF-A11Y-001..004): the
// server's answer is stubbed at the network, so the screen is judged with entries of both targets.

const ANSWER = {
  items: [
    { work_id: 'w1', title: 'Série A', unit_id: 'u2', number: '2', unit_title: null, state: 'next', target: 'reader', approximate: true, carrier_type: 'content', carrier_id: 'c2' },
    { work_id: 'w2', title: 'Série B', unit_id: 'u9', number: '9', unit_title: null, state: 'in_progress', target: 'kavita', approximate: false, carrier_type: null, carrier_id: null }
  ],
  kavita_url: 'http://kavita.example.test:5000'
};

// req: EF-LEC-008
test('test_continue_reading_home_axe', async ({ page }) => {
  await signInAs(page, 'admin');
  await page.route('**/api/reader/continue*', (route) => route.fulfill({ json: ANSWER }));
  await page.goto('/');
  await page.getByRole('region', { name: 'Reprendre la lecture' }).waitFor();
  await expectScreenAccessible(page);
  for (const size of [{ width: 1280, height: 800 }, { width: 360, height: 780 }]) {
    await page.setViewportSize(size);
    await expectNoHorizontalScroll(page);
    // each entry is one control of at least 44 px, named by the series it opens
    for (const link of await page.getByRole('region', { name: 'Reprendre la lecture' }).getByRole('link').all()) {
      const box = await link.boundingBox();
      expect(box?.height ?? 0).toBeGreaterThanOrEqual(44);
      expect(box?.width ?? 0).toBeGreaterThanOrEqual(44);
    }
  }
  await expect(page.getByRole('link', { name: 'Lire, Série A, n° 2' })).toHaveAttribute('href', '/lire/content/c2');
});
