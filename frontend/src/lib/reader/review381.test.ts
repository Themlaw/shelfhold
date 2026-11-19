// #381, adverse review: what the first review of L12 did not see. Each `it.fails` is a red proof
// of a gap that stays open; it turns green (and the suite red) the day the gap is closed, and is
// then rewritten as a plain test.
import { describe, expect, it } from 'vitest';
import { filterMarkup } from './filter.ts';

const ORIGIN = 'https://shelfhold.test';
const LOADER = `blob:${ORIGIN}/6f1c2d7e-0000-4000-8000-000000000001`;
const XHTML = 'application/xhtml+xml';

const page = (body: string) =>
  `<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml"><head><title>t</title></head><body>${body}</body></html>`;

describe('#381 adverse review', () => {
  // req: ENF-SEC-004
  // Foliate rewrites `[*|href]:not([href])` into a `blob:` of the book (epub.js, `loadReplaced`), the
  // filter keeps a loader `blob:` on an SVG `<a xlink:href>`, but foliate only intercepts
  // `closest('a[href]')` (view.js, `#handleLinks`): a click navigates the reading frame to the
  // document of the book, out of foliate (no pagination, no link interception, no key relay,
  // positions frozen). No script runs (inherited policy, `frame-src blob:`, sandbox with no popup
  // nor top navigation): a gap of the reader, not of the barrier.
  it.fails('test_kept_link_is_one_foliate_intercepts: an SVG <a xlink:href> keeps a loader blob that no click handler sees', () => {
    const out = filterMarkup(
      page(`<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"><a xlink:href="${LOADER}"><text id="t">go</text></a></svg>`),
      XHTML,
      { origin: ORIGIN }
    );
    const doc = new DOMParser().parseFromString(out, XHTML);
    const link = doc.getElementById('t')?.parentElement;
    const navigates = link?.getAttributeNS('http://www.w3.org/1999/xlink', 'href') ?? null;
    const intercepted = doc.getElementById('t')?.closest('a[href]') ?? null;
    // what the filter keeps as an address on a link, foliate must see: either no address, or an `href`
    expect(navigates === null || navigates.startsWith('#') || intercepted !== null).toBe(true);
  });
});
