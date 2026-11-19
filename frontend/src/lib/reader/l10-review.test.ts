// The review of the image reader (M5 L10, EF-LEC-001, EF-LEC-002): three proofs, red on the lot
// as delivered. `it.fails` keeps a proof red until its hole is closed; the test that carries no
// mark is the control of the proof beside it: it reads the manifest exactly as the server writes
// it and shows that only the behaviour the proof pins is broken. See `docs/decisions/M5.md`,
// section « L10 ».
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { kitCsp, readerCsp } from '../../../scripts/csp.ts';
import type { Locator, PublicationView } from './engine.ts';
import { createImageEngine } from './engines/images.ts';

/** The manifest as the reader route answers it: the server numbers its entries from 1
 *  (`backend/tests/integration/test_m5_l06_publication.py` freezes `[1, 2, 3, 4]`). Every mock of
 *  the lot — `images.test.ts`, `page.test.ts`, the e2e spec — numbers them from 0, which is why
 *  none of them sees what the proof about the webtoon pins. */
function serverPublication(over: Partial<PublicationView> = {}, count = 10): PublicationView {
  return {
    kind: 'pages',
    scope: 'full',
    readingDirection: 'ltr',
    title: 'Une série',
    language: 'fr',
    spine: [],
    package: null,
    pages: Array.from({ length: count }, (_, i) => ({
      index: i + 1, // from 1: what the server writes, never what the engine may take for a place
      href: `/content/TICKET/p${i + 1}.jpg`,
      mediaType: 'image/jpeg'
    })),
    ...over
  };
}

/** A fetch that answers every page, and the object URLs it hands out, kept in a set. */
function bench(size = 1024) {
  const asked: string[] = [];
  const fetcher = vi.fn<typeof fetch>(async (input) => {
    asked.push(String(input));
    return new Response(new Uint8Array(size), { status: 200, headers: { 'Content-Type': 'image/jpeg' } });
  });
  let n = 0;
  const live = new Set<string>();
  const createObjectURL = vi.fn(() => {
    const url = `blob:review/${(n += 1)}`;
    live.add(url);
    return url;
  });
  const revokeObjectURL = vi.fn((url: string) => void live.delete(url));
  return { asked, fetcher, live, createObjectURL, revokeObjectURL };
}

const flush = async () => {
  for (let i = 0; i < 8; i += 1) await Promise.resolve();
  await new Promise((resolve) => setTimeout(resolve, 0));
};

async function opened(pub: PublicationView, size = 1024) {
  const b = bench(size);
  const host = document.createElement('div');
  document.body.append(host);
  const engine = createImageEngine({
    fetch: b.fetcher,
    createObjectURL: b.createObjectURL,
    revokeObjectURL: b.revokeObjectURL
  });
  const seen: Locator[] = [];
  engine.onLocator((l) => seen.push(l));
  await engine.open(pub, host);
  await flush();
  return { engine, host, seen, ...b };
}

/** What a policy lets an image load: its `img-src`, or `default-src` when it has none. */
function imageSources(csp: string): string[] {
  const directives = csp
    .split(';')
    .map((part) => part.trim())
    .filter(Boolean);
  const named = (name: string) =>
    directives.find((part) => part.toLowerCase().startsWith(`${name} `));
  const chosen = named('img-src') ?? named('default-src');
  return chosen === undefined ? [] : chosen.split(/\s+/).slice(1);
}

afterEach(() => {
  document.body.replaceChildren();
});

describe('the review of the image reader (L10)', () => {
  // ENF-SEC-004: the engine draws every page as a `blob:` image (`engines/images.ts`), so the
  // policy of the page the reader is mounted in must let one load. The route of L10 is a route of
  // the SPA, so it carries the CSP of the SPA — and that policy has no `img-src`: an image falls
  // back to `default-src 'self'`, a source that never admits `blob:`. In a real browser every
  // page of every book is refused, and the screen never says so (the engine does not listen to
  // the image's error). ADR-0055 reserves `img-src 'self' blob:` for the reader and its own
  // entry, with a guard (`test_reader_refuses_to_mount_under_spa_csp`, L12) that does not exist
  // yet; the e2e spec asserts boxes and attributes, never a decoded pixel, so it stays green
  // with every image refused.
  // req: ENF-SEC-004
  it('the policy of the page the reader mounts in lets its blob: images load', () => {
    // L12: the reader is its own entry, and the policy of that page is the one that counts
    const page = `<!doctype html><head><script>import('/reader-entry/assets/main.js');</script></head><body></body>`;
    expect(imageSources(Object.entries(readerCsp(page)).map(([name, sources]) => `${name} ${sources.join(' ')}`).join('; '))).toEqual(["'self'", 'blob:']);
    const spa = Object.entries(kitCsp(readFileSync(resolve(fileURLToPath(import.meta.url), '../../../app.html'), 'utf-8')))
      .map(([name, sources]) => `${name} ${sources.join(' ')}`)
      .join('; ');
    expect(imageSources(spa)).not.toContain('blob:'); // nothing of the SPA draws one
  });

  // req: EF-LEC-001 — the control of the proof below: the manifest of the server, read page by
  // page, is announced and written by the page's place in the book, never by the number its
  // entries carry. This holds today.
  it('a paged book is announced by its place, whatever the manifest numbers its entries', async () => {
    const { engine, host, seen } = await opened(serverPublication());
    expect(host.querySelector('img')?.getAttribute('alt')).toBe('Page 1 sur 10');
    engine.next();
    await flush();
    expect(host.querySelector('img')?.getAttribute('alt')).toBe('Page 2 sur 10');
    expect(seen.at(-1)).toEqual({ v: 1, type: 'page', index: 1, count: 10 });
  });

  // req: EF-LEC-002 — the webtoon column takes the manifest's own number for a place in its array
  // (`dataset.page = String(page.index)`), and the observer of the middle line reports that
  // number. With the manifest the server writes (its entries counted from 1), a page the reader
  // reaches is written one page ahead of the page on screen, and a page that was never shown
  // keeps the label the mount gave it from that number — the last of ten is announced « Page 11
  // sur 10 ». The locator of ADR-0055 counts from 0.
  it('a webtoon is announced and written by the page on screen, not one ahead', async () => {
    const { engine, host, seen } = await opened(serverPublication({ readingDirection: 'webtoon' }));
    const first = host.querySelector('img');
    if (first === null) throw new Error('the column holds no image to read');
    engine.reportVisible(Number(first.dataset.page)); // what the observer of the middle line does
    expect(seen.at(-1)).toEqual({ v: 1, type: 'page', index: 0, count: 10 }); // red: index 1
    const last = host.querySelector('img:last-child');
    expect(last?.getAttribute('alt')).toBe('Page 10 sur 10'); // red: « Page 11 sur 10 »
  });

  // req: EF-LEC-001 — `open()` resets every state of the engine as if a new reading were meant,
  // but the pages it held are dropped with their object URLs still alive. A reading opened again
  // leaks the bytes of the one before it for the life of the document. (The screen of L10 opens
  // one book and frees it at the unmount; the port it calls, which resets itself for the next
  // reading, says otherwise.)
  it('opening a book again frees what the reading before it held', async () => {
    const b = bench(2048);
    const host = document.createElement('div');
    document.body.append(host);
    const engine = createImageEngine({
      fetch: b.fetcher,
      createObjectURL: b.createObjectURL,
      revokeObjectURL: b.revokeObjectURL
    });
    await engine.open(serverPublication({}, 6), host);
    await flush();
    expect(b.live.size).toBeGreaterThan(0); // the first reading holds pages: this holds today
    await engine.open(serverPublication({}, 6), host); // the port allows it: every state is reset
    await flush();
    engine.destroy();
    expect(b.live.size).toBe(0); // red: the first reading's URLs were never revoked
  });
});
