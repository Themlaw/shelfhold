import { describe, expect, it } from 'vitest';
import type { PublicationOut } from '#lib/api/types.gen.ts';
import { toView } from './engine.ts';

// The manifest as the real server writes it: `publication.py` numbers its entries from 1, and
// `backend/tests/integration/test_m5_l06_publication.py` freezes `[1, 2, 3, 4]`. This is that shape.
const server: PublicationOut = {
  carrier_id: '01HZZZZZZZZZZZZZZZZZZZZZZC',
  carrier_type: 'content',
  work_id: '01HZZZZZZZZZZZZZZZZZZZZZZW',
  expires_at: '2026-11-18T12:10:00Z',
  kind: 'pages',
  language: 'ja',
  number: '1',
  package: null,
  pages: [4, 2, 1, 3].map((n) => ({ index: n, href: `/content/T/p${n}.jpg`, media_type: 'image/jpeg' })),
  reading_direction: 'rtl',
  scope: 'full',
  spine: [],
  title: 'Une série'
};

describe('the view of a publication', () => {
  // req: EF-LEC-001
  it('puts the entries of the server, counted from 1, in their place from 0, in reading order', () => {
    const view = toView(server);
    expect(view.pages.map((p) => p.index)).toEqual([0, 1, 2, 3]);
    expect(view.pages.map((p) => p.href)).toEqual([1, 2, 3, 4].map((n) => `/content/T/p${n}.jpg`));
    expect(view.readingDirection).toBe('rtl');
  });
});
