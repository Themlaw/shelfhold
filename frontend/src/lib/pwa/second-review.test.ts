// @vitest-environment node
// The second adverse review of L16a (the installable shell): a red proof of the gap that stays
// open. An `it.fails` is a gap: it turns green (and the suite red) the day the gap is closed, and
// is then rewritten as a plain test. The server half of the same gap is the strict `xfail` of
// `backend/tests/integration/test_m5_l16a_second_review.py`.
import { describe, expect, it } from 'vitest';
import { isReserved } from './worker.ts';

describe('the second adverse review of L16a', () => {
  // req: EF-LEC-012, ENF-SEC-004
  // The server decodes the request line and decides "this is the reader" on the raw first segment
  // of the decoded path: `/lire%2F..` reaches it as `/lire/..` and the reader entry is served
  // (measured against the app under a real ASGI server). The worker decodes the same way, but then
  // resolves the dot segments (`isReserved`: `..` pops `lire`), so the same request line is not
  // reserved to it: offline, the fetch handler answers the navigation with the kept shell of the
  // SPA — the shell in place of the reader, and less than the server serves as the reader, which
  // the lot claimed never happens.
  it.fails(
    'test_offline_reader_entry_keeps_reader_csp: a request line the server serves as the reader is reserved',
    () => {
      for (const path of ['/lire%2F..', '/lire%2F..%2Fx', '/lire%2F%2e%2e']) {
        expect(isReserved(path), path).toBe(true);
      }
    }
  );
});
