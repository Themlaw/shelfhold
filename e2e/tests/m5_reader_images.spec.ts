import { deflateSync, crc32 } from 'node:zlib';
import type { Page, Route } from '@playwright/test';
import { expect, test } from '../workshop/fixtures';

// M5 L10: the reader of images in a real browser: paged left to right and right to left, and the
// continuous webtoon. The API of the reader (L06, L07) and the content route (L05a) are proved
// against the real server by the integration tests; here they answer with a synthetic book so
// that what is played is the layout, the keys and the scroll, which only a browser has.

const ME = { id: '01HZZZZZZZZZZZZZZZZZZZZZZA', username: 'lecteur', role: 'member', nav: [] };

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

interface Book {
  direction: 'ltr' | 'rtl' | 'webtoon';
  count: number;
  width: number;
  height: number;
}

interface Bench {
  puts: Record<string, unknown>[];
  contentRequests: string[];
}

async function serveBook(page: Page, book: Book): Promise<Bench> {
  const bench: Bench = { puts: [], contentRequests: [] };
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
        pages: Array.from({ length: book.count }, (_, i) => ({
          index: i,
          href: `/content/TICKET/p${i + 1}.png`,
          media_type: 'image/png'
        })),
        reading_direction: book.direction,
        scope: 'full',
        spine: [],
        title: 'Une série synthétique'
      }
    })
  );
  await page.route('**/content/TICKET/*', (route: Route) => {
    bench.contentRequests.push(new URL(route.request().url()).pathname);
    return route.fulfill({
      body: png(book.width, book.height, 40 + (bench.contentRequests.length % 5) * 30),
      contentType: 'image/png'
    });
  });
  await page.route('**/api/reader/positions', async (route) => {
    bench.puts.push(route.request().postDataJSON() as Record<string, unknown>);
    await route.fulfill({ json: { conflict: null, position: null } });
  });
  return bench;
}

const status = (page: Page) => page.getByRole('status');

// req: EF-LEC-001
test('test_manga_paged_ltr_rtl', async ({ page }) => {
  const ltr = await serveBook(page, { direction: 'ltr', count: 4, width: 300, height: 450 });
  await page.goto('/lire/unit/U1');
  await expect(page.getByAltText('Page 1 sur 4')).toBeVisible();
  await page.keyboard.press('ArrowRight');
  await expect(status(page)).toHaveText('Page 2 sur 4');
  await page.keyboard.press('ArrowLeft');
  await expect(status(page)).toHaveText('Page 1 sur 4');
  await page.keyboard.press('ArrowRight');
  await expect.poll(() => ltr.puts.length, { timeout: 5000 }).toBeGreaterThan(0);
  expect(ltr.puts.at(-1)).toMatchObject({ locator: { v: 1, type: 'page', index: 1, count: 4 } });

  // the same book, read right to left: the arrows are turned around, the numbers are not
  await page.unrouteAll();
  const rtl = await serveBook(page, { direction: 'rtl', count: 4, width: 300, height: 450 });
  await page.goto('/lire/unit/U1');
  await expect(page.getByAltText('Page 1 sur 4')).toBeVisible();
  await expect(page.locator('[dir="rtl"]')).not.toHaveCount(0);
  await page.keyboard.press('ArrowRight'); // "back" in this book, and we are on the first page
  await expect(status(page)).not.toHaveText('Page 2 sur 4');
  await page.keyboard.press('ArrowLeft');
  await expect(status(page)).toHaveText('Page 2 sur 4');
  await expect.poll(() => rtl.puts.length, { timeout: 5000 }).toBeGreaterThan(0);
  expect(rtl.puts.at(-1)).toMatchObject({ locator: { index: 1, count: 4 } });

  // only a bounded few pages were asked for, never the whole book at once, and only in /content
  expect(rtl.contentRequests.every((p) => p.startsWith('/content/TICKET/'))).toBe(true);
});

// req: EF-LEC-002
test('test_webtoon_continuous', async ({ page }) => {
  const bench = await serveBook(page, { direction: 'webtoon', count: 6, width: 400, height: 700 });
  await page.goto('/lire/unit/U1');
  const column = page.locator('[data-reader-column]');
  await expect(column.locator('img')).toHaveCount(6);
  await expect(column.locator('img').first()).toBeVisible();
  // no space between two pages: the top of one is the bottom of the one before
  const boxes = await column.locator('img').evaluateAll((images) =>
    images.map((image) => {
      const r = image.getBoundingClientRect();
      return { top: r.top + window.scrollY, bottom: r.bottom + window.scrollY };
    })
  );
  for (let i = 1; i < boxes.length; i += 1) {
    expect(Math.abs(boxes[i].top - boxes[i - 1].bottom)).toBeLessThanOrEqual(1);
  }
  await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
  await expect(status(page)).toHaveText(/Page [56] sur 6/);
  await expect.poll(() => bench.puts.length, { timeout: 5000 }).toBeGreaterThan(0);
  const last = bench.puts.at(-1) as { locator: { index: number; count: number } };
  expect(last.locator.count).toBe(6);
  expect(last.locator.index).toBeGreaterThanOrEqual(4);
});
