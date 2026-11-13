import { readFileSync } from 'node:fs';
import { expect, signInAs, test } from './fixtures';

// ENF-PERF-010: a library screen of 60 covers weighs at most 1.5 MB. The bytes counted are those of
// every response the browser receives while the screen loads, from a cold browser cache: the page,
// its scripts and styles, the list and the 60 thumbnails (L24) that the grid shows (320 px: a card
// of 160 CSS px on a screen at twice the density). The covers are those of the synthetic base
// (`seed.py`: 60 Works, seeded, drawn to weigh what a real cover does). The budget is the spec's
// and is never loosened here.
// Read from the versioned reference, where the bench tests pin it to the spec's number.
const { api } = JSON.parse(readFileSync('../.github/bench-reference.json', 'utf-8')).budgets;
const BUDGET_BYTES: number = api.screen_max_bytes;
const COVERS: number = api.screen_covers;
const GRID_SIZE = 320;
const MADE = /immutable/;

// Written by `seed.py` once the base is made, which is after the test files are loaded.
const readDigests = (): string[] => JSON.parse(readFileSync('.tmp-workshop/covers.json', 'utf-8'));
const thumbnail = (digest: string) => `/api/v1/thumbnails/${digest}/${GRID_SIZE}`;

// req: ENF-PERF-010
test('test_library_screen_weight_under_1_5mb_for_60_covers', async ({ page }) => {
  const digests = readDigests();
  expect(digests).toHaveLength(COVERS);
  await signInAs(page, 'admin');

  // A thumbnail is made on first use and kept: let the catch-up finish, so that what is measured
  // is the screen in its steady state (a cover still being made is answered, uncached, by the
  // cover itself, which is 10 times heavier).
  await expect
    .poll(
      async () => {
        let made = 0;
        for (const digest of digests) {
          const answer = await page.request.get(thumbnail(digest));
          if (answer.ok() && MADE.test(answer.headers()['cache-control'] ?? '')) made += 1;
        }
        return made;
      },
      { timeout: 60_000, intervals: [500] }
    )
    .toBe(COVERS);

  // The measured load: a fresh page (cold HTTP cache), the list the screen asks for, then the
  // grid of 60 images, nothing lazy.
  let transferred = 0;
  const seen: string[] = [];
  page.on('response', async (response) => {
    const body = await response.body().catch(() => Buffer.alloc(0));
    transferred += body.length;
    seen.push(new URL(response.url()).pathname);
  });
  await page.goto('/');
  await page.evaluate(
    async ({ urls, list }) => {
      const answer = await fetch(list);
      if (!answer.ok) throw new Error(`list refused: ${answer.status}`);
      await answer.json();
      await Promise.all(
        urls.map(
          (src) =>
            new Promise<void>((resolve, reject) => {
              const img = new Image();
              img.onload = () => resolve();
              img.onerror = () => reject(new Error(`cover not shown: ${src}`));
              img.src = src;
              document.body.append(img);
            })
        )
      );
    },
    { urls: digests.map(thumbnail), list: `/api/v1/works?limit=${COVERS}` }
  );
  await page.waitForLoadState('networkidle');

  const covers = seen.filter((path) => path.startsWith('/api/v1/thumbnails/'));
  expect(seen).toContain('/api/v1/works'); // the list was fetched, and counted
  expect(covers).toHaveLength(COVERS); // the 60 were really fetched, and counted
  expect(transferred).toBeGreaterThan(covers.length * 1024); // thumbnails, not empty bodies
  expect(transferred, `${(transferred / 1024).toFixed(0)} KiB over ${seen.length} responses`).toBeLessThanOrEqual(
    BUDGET_BYTES
  );
});
