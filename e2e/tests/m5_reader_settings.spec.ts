import { deflateSync, crc32 } from 'node:zlib';
import type { Page, Route } from '@playwright/test';
import { expect, expectScreenAccessible, test } from '../workshop/fixtures';

// M5 L13: the settings of the reader and its keyboard, in a real browser. The API of the reader
// (L06, L07, L13) is proved against the real server by the integration tests; here it answers with
// a synthetic book so that what is played is the keyboard, the focus, the portée asked on a
// change, and what a screen reader holds — which only a browser has.

const ME = { id: '01HZZZZZZZZZZZZZZZZZZZZZZA', username: 'lecteur', role: 'member', nav: [] };
const SERIES = '01HZZZZZZZZZZZZZZZZZZZZZZW';

function chunk(kind: string, data: Buffer): Buffer {
  const head = Buffer.alloc(8);
  head.writeUInt32BE(data.length, 0);
  head.write(kind, 4, 'ascii');
  const tail = Buffer.alloc(4);
  tail.writeUInt32BE(crc32(Buffer.concat([head.subarray(4), data])), 0);
  return Buffer.concat([head, data, tail]);
}

/** A plain grey PNG: a page that is only a size and a shade. */
function png(width: number, height: number, shade: number): Buffer {
  const row = Buffer.concat([Buffer.from([0]), Buffer.alloc(width * 3, shade)]);
  const header = Buffer.alloc(13);
  header.writeUInt32BE(width, 0);
  header.writeUInt32BE(height, 4);
  header.set([8, 2, 0, 0, 0], 8);
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk('IHDR', header),
    chunk('IDAT', deflateSync(Buffer.concat(Array.from({ length: height }, () => row)))),
    chunk('IEND', Buffer.alloc(0))
  ]);
}

interface Bench {
  settingsPuts: Record<string, unknown>[];
  effective: { direction: string | null; comfort: Record<string, string> };
}

async function serveBook(page: Page, direction: 'ltr' | 'rtl' | 'webtoon'): Promise<Bench> {
  const bench: Bench = { settingsPuts: [], effective: { direction: null, comfort: {} } };
  await page.route('**/api/v1/me', (route) => route.fulfill({ json: ME }));
  await page.route('**/api/reader/unit/U1/publication*', (route) =>
    route.fulfill({
      json: {
        carrier_id: '01HZZZZZZZZZZZZZZZZZZZZZZC',
        carrier_type: 'content',
        expires_at: '2099-01-01T00:00:00Z',
        kind: 'pages',
        language: 'ja',
        number: '1',
        package: null,
        pages: Array.from({ length: 4 }, (_, i) => ({
          index: i,
          href: `/content/TICKET/p${i + 1}.png`,
          media_type: 'image/png'
        })),
        reading_direction: direction,
        scope: 'full',
        series_id: SERIES,
        spine: [],
        title: 'Une série synthétique'
      }
    })
  );
  await page.route('**/content/TICKET/*', (route: Route) =>
    route.fulfill({ body: png(300, 450, 120), contentType: 'image/png' })
  );
  await page.route('**/api/reader/positions', (route) =>
    route.fulfill({ json: { conflict: null, position: null } })
  );
  await page.route('**/api/reader/settings', (route) => {
    if (route.request().method() === 'PUT') {
      const body = (route.request().postDataJSON() ?? {}) as Record<string, unknown>;
      bench.settingsPuts.push(body);
      if (body.kind === 'direction') bench.effective.direction = String(body.value);
      else bench.effective.comfort[String(body.key)] = String(body.value);
    }
    return route.fulfill({ json: bench.effective });
  });
  return bench;
}

const status = (page: Page) => page.getByRole('status');

// req: EF-LEC-011, ENF-A11Y-002
test('test_keyboard_shortcuts_reader', async ({ page }) => {
  await serveBook(page, 'ltr');
  await page.goto('/lire/unit/U1');
  await expect(page.getByAltText('Page 1 sur 4')).toBeVisible();

  // the arrows follow the sens, Space turns, Home and End reach the two ends
  await page.keyboard.press('ArrowRight');
  await expect(status(page)).toHaveText('Page 2 sur 4');
  await page.keyboard.press(' ');
  await expect(status(page)).toHaveText('Page 3 sur 4');
  await page.keyboard.press('Home');
  await expect(status(page)).toHaveText('Page 1 sur 4');
  await page.keyboard.press('End');
  await expect(status(page)).toHaveText('Page 4 sur 4');

  // ? opens the help, Escape closes it and gives the focus back to the controls
  await page.keyboard.press('?');
  await expect(page.getByRole('heading', { name: 'Aide du lecteur' })).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('heading', { name: 'Aide du lecteur' })).toBeHidden();
  await expect(page.getByRole('button', { name: 'Quitter la lecture' })).toBeFocused();

  // the tab order of the ADR: the bar, then the pagination; the focus is visible, never trapped
  // (the focus stands on the first control of the bar: the cycle starts after it and comes back)
  const stops: string[] = [];
  for (let i = 0; i < 7; i += 1) {
    await page.keyboard.press('Tab');
    const stop = await page.evaluate(() => {
      const el = document.activeElement as HTMLElement | null;
      if (!el) return null;
      if (el === document.body) return { body: true as const };
      const style = getComputedStyle(el);
      return {
        name: (el.getAttribute('aria-label') ?? el.textContent ?? '').trim(),
        ring: style.outlineStyle !== 'none' && parseFloat(style.outlineWidth) > 0
      };
    });
    if (stop === null) break;
    if ('body' in stop) continue; // the Tab left the page for the browser: not a stop of the reader
    if (stops.length > 0 && stop.name === stops[0]) break; // back on the first stop: no trap
    stops.push(stop.name);
    expect(stop.ring, `the focus ring is visible on ${stop.name}`).toBe(true);
  }
  expect(stops).toEqual([
    'Réglages de lecture',
    'Lire hors ligne',
    'Aide du lecteur',
    'Page précédente',
    'Page suivante',
    'Quitter la lecture'
  ]);

  // the same book read right to left: the arrows are turned around, the numbers are not
  await page.unrouteAll();
  await serveBook(page, 'rtl');
  await page.goto('/lire/unit/U1');
  await expect(page.getByAltText('Page 1 sur 4')).toBeVisible();
  await page.keyboard.press('ArrowLeft');
  await expect(status(page)).toHaveText('Page 2 sur 4');
  await page.keyboard.press('ArrowRight');
  await expect(status(page)).toHaveText('Page 1 sur 4');
});

// req: EF-LEC-006
test('test_reader_settings_scope_is_asked_on_change', async ({ page }) => {
  const bench = await serveBook(page, 'ltr');
  await page.goto('/lire/unit/U1');
  await expect(page.getByAltText('Page 1 sur 4')).toBeVisible();

  await page.getByRole('button', { name: 'Réglages de lecture' }).click();
  await page.getByRole('radio', { name: 'Pleine largeur' }).check();
  await expect(page.getByText('Pour cet appareil ou partout ?')).toBeVisible();
  await page.getByRole('button', { name: 'Partout' }).click();
  await expect.poll(() => bench.settingsPuts.length).toBe(1);
  expect(bench.settingsPuts[0]).toMatchObject({
    kind: 'comfort',
    key: 'fit',
    value: 'width',
    scope: 'everywhere'
  });

  // the sens of the series is written for the series, and the book is drawn again
  await page.getByRole('radio', { name: 'De droite à gauche' }).check();
  await expect.poll(() => bench.settingsPuts.length).toBe(2);
  expect(bench.settingsPuts[1]).toMatchObject({
    kind: 'direction',
    key: SERIES,
    value: 'rtl',
    scope: 'everywhere'
  });
  await expect(page.locator('main.reader')).toHaveAttribute('data-direction', 'rtl');
});

// req: ENF-A11Y-002
test('test_axe_reader_screen', async ({ page }) => {
  await serveBook(page, 'ltr');
  await page.goto('/lire/unit/U1');
  await expect(page.getByAltText('Page 1 sur 4')).toBeVisible();
  await expectScreenAccessible(page);
  // the settings panel and the help are part of the reader: they are held to the same rule
  await page.getByRole('button', { name: 'Réglages de lecture' }).click();
  await page.keyboard.press('?');
  await expect(page.getByRole('heading', { name: 'Aide du lecteur' })).toBeVisible();
  await expectScreenAccessible(page);
});
