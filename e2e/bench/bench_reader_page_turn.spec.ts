import { deflateSync, crc32 } from 'node:zlib';
import { mkdirSync, writeFileSync } from 'node:fs';
import type { Page, Route } from '@playwright/test';
import { expect, test } from '@playwright/test';

// ENF-PERF-006 (M5 L15): the turn of a page of the image reader, from the action "next page" to
// the next page displayed, must stay under 150 ms once the preload has filled the cache. The
// server side of the reader is proved by the integration tests; here the real SPA is drawn by a
// real browser and only the book is synthetic (routes, like tests/m5_reader_images.spec.ts): the
// page is the reference profile's size (ADR-0004), a solid shade, so what the turn pays is the
// decode, not the bytes. The measure lives in the page (performance.now): from outside, the
// round trips of the driver would be measured, not the reader.

const ME = { id: '01HZZZZZZZZZZZZZZZZZZZZZZA', username: 'lecteur', role: 'member', nav: [] };
const PAGES = 40; // 30 measured turns, 3 pages of preload ahead, room to spare
const WIDTH = 1600;
const HEIGHT = 2400;
const TURNS = 30;
const BUDGET_MS = 150;
const REPORT = 'test-results/bench_reader_page_turn.json';

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

/** The pages the engine has asked for, by their number in the href (`p3.png` → 3). */
async function serveBook(page: Page): Promise<Set<number>> {
  const asked = new Set<number>();
  const body = png(WIDTH, HEIGHT, 128);
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
        pages: Array.from({ length: PAGES }, (_, i) => ({
          index: i + 1, // the server counts the entries of a manifest from 1
          href: `/content/TICKET/p${i + 1}.png`,
          media_type: 'image/png'
        })),
        reading_direction: 'ltr',
        scope: 'full',
        spine: [],
        title: 'Une série synthétique'
      }
    })
  );
  await page.route('**/content/TICKET/*', (route: Route) => {
    asked.add(Number(/p(\d+)\.png$/.exec(new URL(route.request().url()).pathname)?.[1]));
    return route.fulfill({ body, contentType: 'image/png' });
  });
  await page.route('**/api/reader/positions', (route) =>
    route.fulfill({ json: { conflict: null, position: null } })
  );
  return asked;
}

/** Nearest-rank percentile, like the backend benches (tests/bench/harness.py): the smallest
 * sample that at least `fraction` of the samples do not exceed — never an average of two, so a
 * slow turn is never smoothed away. */
function percentile(samples: number[], fraction: number): number {
  const sorted = [...samples].sort((a, b) => a - b);
  return sorted[Math.min(sorted.length, Math.ceil(fraction * sorted.length)) - 1];
}

/** Arms the measure in the page: the next ArrowRight starts the clock at the keydown (capture,
 * before the reader's own handler), the clock stops when the image on screen is the next page,
 * decoded (`naturalWidth > 0`). Resolves with the duration in ms, or -1 if nothing came. */
async function armTurn(page: Page): Promise<void> {
  await page.evaluate(() => {
    const w = window as unknown as { __turn?: Promise<number> };
    w.__turn = new Promise<number>((resolve) => {
      const img = document.querySelector('[data-reader-page] img') as HTMLImageElement | null;
      if (!img) {
        resolve(-1);
        return;
      }
      const before = img.src;
      let t0 = 0;
      window.addEventListener(
        'keydown',
        (event) => {
          if (event.key === 'ArrowRight') t0 = performance.now();
        },
        { capture: true, once: true }
      );
      const started = performance.now();
      const tick = () => {
        if (t0 > 0 && img.src !== before && img.complete && img.naturalWidth > 0) {
          resolve(performance.now() - t0);
        } else if (performance.now() - started > 10_000) {
          resolve(-1);
        } else {
          requestAnimationFrame(tick);
        }
      };
      requestAnimationFrame(tick);
    });
  });
}

const turnMs = (page: Page) =>
  page.evaluate(() => (window as unknown as { __turn?: Promise<number> }).__turn);

// req: ENF-PERF-006
test('bench_reader_page_turn', async ({ page }) => {
  const asked = await serveBook(page);
  await page.goto('/lire/unit/U1');
  await expect(page.getByAltText(`Page 1 sur ${PAGES}`)).toBeVisible();
  await expect
    .poll(() => (page.locator('[data-reader-page] img').evaluate((img) => (img as HTMLImageElement).naturalWidth)))
    .toBeGreaterThan(0);

  const samples: number[] = [];
  for (let turn = 1; turn <= TURNS; turn += 1) {
    await armTurn(page);
    await page.keyboard.press('ArrowRight');
    const ms = await turnMs(page);
    expect(ms, `turn ${turn}: the next page was displayed`).toBeGreaterThan(0);
    samples.push(ms);
  }

  const p50 = percentile(samples, 0.5);
  const p95 = percentile(samples, 0.95);
  const report = {
    schema: 1,
    requirement: 'ENF-PERF-006',
    bench: 'bench_reader_page_turn',
    fixture: `synthetic paged book, ${PAGES} pages of ${WIDTH}x${HEIGHT}, solid shade (ADR-0004 reference dimensions)`,
    turns: TURNS,
    p50_ms: p50,
    p95_ms: p95,
    max_ms: Math.max(...samples),
    samples_ms: samples,
    budget_p95_ms: BUDGET_MS
  };
  mkdirSync('test-results', { recursive: true });
  writeFileSync(REPORT, `${JSON.stringify(report, null, 2)}\n`);
  expect(
    p95,
    `ENF-PERF-006: the turn of a page from the cache stays under ${BUDGET_MS} ms (p95 over ${TURNS} turns)`
  ).toBeLessThanOrEqual(BUDGET_MS);
});
