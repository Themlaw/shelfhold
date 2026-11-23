import { deflateSync, crc32 } from 'node:zlib';
import type { Page } from '@playwright/test';
import { expect, test } from '../workshop/fixtures';

// M6 L20 (EF-LEC-012, D-P7 (b)): « Lire hors ligne » in the reader, in a real browser with the real
// stores (IndexedDB, Cache Storage, Web Locks) — the API answers from routes, as the reader specs of
// M5 do, because what is played here is what only a browser does. A copy is kept in one tab while
// another signs a child in: the write of the copy is locked across tabs, it waits for the wipe, sees
// that the profile is no longer the browser's and drops the bytes — nothing of the profile that
// left (cache or database) stays behind. The pages the reader draws are grey PNGs.

const MEMBER = { id: '01JMEMBER0000000000000000M', username: 'membre', role: 'member', nav: [] };
const CHILD = { id: '01JCHILD00000000000000000C', username: 'enfant8', role: 'child', nav: [] };
const CARRIER = '01HZZZZZZZZZZZZZZZZZZZZZZC';

function chunk(kind: string, data: Buffer): Buffer {
  const head = Buffer.alloc(8);
  head.writeUInt32BE(data.length, 0);
  head.write(kind, 4, 'ascii');
  const tail = Buffer.alloc(4);
  tail.writeUInt32BE(crc32(Buffer.concat([head.subarray(4), data])), 0);
  return Buffer.concat([head, data, tail]);
}

/** A plain grey PNG: a page that is only a size and a shade. */
function png(shade: number): Buffer {
  const width = 120;
  const height = 180;
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

const publication = {
  carrier_id: CARRIER,
  carrier_type: 'content',
  expires_at: '2099-01-01T00:00:00Z',
  kind: 'pages',
  language: 'fr',
  number: '1',
  package: null,
  pages: [1, 2, 3].map((n) => ({ index: n, href: `/content/TICKET/p${n}.png`, media_type: 'image/png' })),
  reading_direction: 'ltr',
  scope: 'full',
  series_id: '01HZZZZZZZZZZZZZZZZZZZZZZW',
  spine: [],
  title: 'Une série synthétique',
  work_id: '01HZZZZZZZZZZZZZZZZZZZZZZW'
};

/** What the SPA and the reader ask, answered by routes: `me` names the profile of the tab, `works`
 * gives a `?since=` token, and the last entry of the book waits on a gate the test holds. */
async function serve(
  page: Page,
  me: { id: string; username: string; role: string; nav: string[] },
  gate: Promise<void>
): Promise<void> {
  await page.route('**/api/v1/me', (route) => route.fulfill({ json: me }));
  await page.route('**/api/v1/works**', (route) =>
    route.fulfill({ json: { works: [], since: `tok-${me.id}`, removed: [] } })
  );
  await page.route('**/api/reader/unit/U1/publication*', (route) => route.fulfill({ json: publication }));
  await page.route('**/api/reader/settings**', (route) => route.fulfill({ json: { direction: null, comfort: {} } }));
  await page.route('**/api/reader/positions', (route) => route.fulfill({ json: { conflict: null, position: null } }));
  await page.route('**/content/TICKET/*', (route) => route.fulfill({ body: png(120), contentType: 'image/png' }));
  await page.route('**/content/TICKET/p3.png', async (route) => {
    await gate; // the download of the copy is held on its last entry
    await route.fulfill({ body: png(180), contentType: 'image/png' });
  });
}

/** One value of the key-value store of the offline copies, read straight from IndexedDB. */
function readKv(page: Page, name: string, key: string): Promise<unknown> {
  return page.evaluate(
    ({ name, key }) =>
      new Promise((resolve, reject) => {
        const open = indexedDB.open(name);
        open.onupgradeneeded = () => open.result.createObjectStore('kv');
        open.onsuccess = () => {
          const db = open.result;
          const got = db.transaction('kv', 'readonly').objectStore('kv').get(key);
          got.onsuccess = () => {
            resolve(got.result ?? null);
            db.close();
          };
          got.onerror = () => {
            reject(got.error);
            db.close();
          };
        };
        open.onerror = () => reject(open.error);
      }),
    { name, key }
  );
}

const activeProfile = (page: Page) => readKv(page, 'shelfhold-offline-active', 'profile');
const cacheNames = (page: Page) => page.evaluate(async () => (await caches.keys()).sort());
const databaseNames = (page: Page) =>
  page.evaluate(async () =>
    ((indexedDB as unknown as { databases?: () => Promise<{ name?: string }[]> }).databases?.() ?? Promise.resolve([])).then(
      (dbs) => dbs.map((db) => db.name ?? '')
    )
  );

// req: EF-LEC-012
test('offline writes are locked across tabs', async ({ page, context }) => {
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  await serve(page, MEMBER, gate);

  // the SPA of the first tab signs a member in: the copies of this browser become hers
  await page.goto('/');
  await expect.poll(() => activeProfile(page)).toBe(MEMBER.id);

  // the reader opens her book and keeps a copy: the download begins and holds on its last entry
  await page.goto('/lire/unit/U1');
  await page.getByRole('button', { name: 'Lire hors ligne' }).click();
  await expect.poll(() => cacheNames(page)).toContain(`shelfhold-copy-${MEMBER.id}`);

  // meanwhile, another tab of the same browser signs a child in: the member's copies are wiped
  const other = await context.newPage();
  await serve(other, CHILD, Promise.resolve());
  await other.goto('/');
  await expect.poll(() => activeProfile(other)).toBe(CHILD.id);
  await expect.poll(() => cacheNames(other)).not.toContain(`shelfhold-copy-${MEMBER.id}`);

  // the held download now ends: the write of the copy takes the lock, sees that the browser is the
  // child's, drops the bytes and says it failed — it never writes the copy of a profile that left
  release();
  await expect(page.getByRole('alert')).toContainText('La copie n’a pas pu être gardée');

  // nothing of the member stays: no cache of her copy, no store of her profile
  await expect.poll(() => cacheNames(other)).not.toContain(`shelfhold-copy-${MEMBER.id}`);
  await expect.poll(() => databaseNames(other)).toEqual(
    expect.arrayContaining(['shelfhold-offline-active', `shelfhold-offline-p-${CHILD.id}`])
  );
  expect(await databaseNames(other)).not.toContain(`shelfhold-offline-p-${MEMBER.id}`);
});
