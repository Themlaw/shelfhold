// The adverse reading of M5 L13 (EF-LEC-006, ENF-A11Y-002): what the review found on the screen.
//
// Every test here is a hole found by reading the lot against its own claims: red on the branch
// (`it.fails`), it fails for the reason its message says, and it turns loud the day the hole is
// closed. What the same reading found solid stays covered by the lot's own tests: no shortcut
// acts while the focus is in a field of the settings, Escape gives the focus back from
// anywhere (no trap), every command has a name, and the catalogue speaks both languages.
import { cleanup, render, screen, waitFor } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import Reader from '../../reader/Reader.svelte';

const BASE = 'http://shelfhold.test';
const server = setupServer();

// The reader is mounted as the entry of L12 mounts it: a carrier, a scope, and a policy that
// already said yes (the tests have no header to read).
const props = {
  carrierType: 'unit',
  carrierId: 'U1',
  scope: null,
  guard: async () => 'ok' as const
};

// The manifest of a book, as the server of L06 gives it (the series is the anchor of L13).
const publication = () => ({
  carrier_id: 'C1',
  carrier_type: 'content',
  expires_at: '2026-11-18T12:10:00Z',
  kind: 'pages',
  language: 'fr',
  number: '3',
  package: null,
  pages: [1, 2, 3].map((n) => ({
    index: n - 1,
    href: `/content/TICKET/p${n}.jpg`,
    media_type: 'image/jpeg'
  })),
  reading_direction: 'ltr',
  scope: 'full',
  series_id: '01HZZZZZZZZZZZZZZZZZZZZZZW',
  spine: [],
  title: 'Une série'
});

// What the server answers of the settings: the answer of a write says what is effective now,
// and a comfort write is resolved without the series, so it answers the profile's default
// direction (she wrote none: `null`) — the reader must not take it for the series' own.
let settingsPuts: Record<string, unknown>[] = [];

const serve = () => [
  http.get(`${BASE}/api/reader/unit/U1/publication`, () => HttpResponse.json(publication())),
  http.put(`${BASE}/api/reader/positions`, () =>
    HttpResponse.json({ conflict: null, position: null })
  ),
  http.get(`${BASE}/api/reader/settings`, () => HttpResponse.json({ direction: null, comfort: {} })),
  http.put(`${BASE}/api/reader/settings`, async ({ request }) => {
    const body = (await request.json()) as Record<string, unknown>;
    settingsPuts.push(body);
    const answer: { direction: string | null; comfort: Record<string, string> } = {
      direction: null, // the default of the profile: `PUT` resolves a comfort without the series
      comfort: {}
    };
    if (body.kind === 'direction') answer.direction = String(body.value);
    else answer.comfort[String(body.key)] = String(body.value);
    return HttpResponse.json(answer);
  }),
  http.get(`${BASE}/content/TICKET/:name`, () =>
    new HttpResponse(new Uint8Array(16), { headers: { 'Content-Type': 'image/jpeg' } })
  )
];

beforeAll(() => {
  server.listen({ onUnhandledRequest: 'error' } as never);
  client.setConfig({ baseUrl: BASE });
  const patched = globalThis.fetch; // the one the mock server answers through
  vi.stubGlobal(
    'fetch',
    (input: RequestInfo | URL, init?: RequestInit) =>
      patched(typeof input === 'string' && input.startsWith('/') ? `${BASE}${input}` : input, init)
  );
  vi.stubGlobal(
    'URL',
    Object.assign(URL, {
      createObjectURL: () => `blob:t/${Math.random()}`,
      revokeObjectURL: () => undefined
    })
  );
});
beforeEach(() => {
  document.cookie = 'csrf=tok123';
  settingsPuts = [];
});
afterEach(() => {
  cleanup(); // the reader says where it was when it is left, while its handlers are still there
  server.resetHandlers();
});
afterAll(() => {
  server.close();
  vi.unstubAllGlobals();
});

describe('the settings of the reader, read adversarially', () => {
  // req: EF-LEC-006
  it('a comfort change does not make the open reader forget the direction of the series', async () => {
    // The series is pinned to right-to-left, then the fit is changed and the portée answered.
    // The answer of a comfort write resolves without the series, and the screen takes it as
    // the whole of its settings: the direction the profile pinned to this series is forgotten
    // in the open reader — the radios fall back to the Work's own direction and the keyboard
    // turns the wrong way — although the server still holds the pinned row.
    const user = userEvent.setup();
    server.use(...serve());
    render(Reader, { props });
    expect(await screen.findByAltText('Page 1 sur 3')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Réglages de lecture' }));
    await user.click(screen.getByRole('radio', { name: 'De droite à gauche' }));
    await waitFor(() => expect(settingsPuts).toHaveLength(1));
    expect(settingsPuts[0]).toMatchObject({
      kind: 'direction',
      key: '01HZZZZZZZZZZZZZZZZZZZZZZW',
      value: 'rtl',
      scope: 'everywhere'
    });
    await user.click(screen.getByRole('radio', { name: 'Pleine largeur' }));
    await user.click(screen.getByRole('button', { name: 'Partout' }));
    await waitFor(() => expect(settingsPuts).toHaveLength(2));
    expect(screen.getByRole('radio', { name: 'De droite à gauche' })).toBeChecked();
    await user.keyboard('{ArrowLeft}'); // rtl: the arrow forward
    expect(await screen.findByAltText('Page 2 sur 3')).toBeInTheDocument();
  });

  // req: ENF-A11Y-002
  it('the portée question is announced when it appears', async () => {
    // A comfort change is asked its portée before it is written: the question appears when a
    // radio is picked, and nothing says it did — no live region holds it, so a reader of the
    // screen learns of the question only by wandering into it. A message that appears must be
    // announced (a `status` or an `alert`), as the page counter already is.
    const user = userEvent.setup();
    server.use(...serve());
    render(Reader, { props });
    expect(await screen.findByAltText('Page 1 sur 3')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Réglages de lecture' }));
    await user.click(screen.getByRole('radio', { name: 'Pleine largeur' }));
    const question = await screen.findByText('Pour cet appareil ou partout ?');
    expect(question.closest('[role="status"], [role="alert"], [aria-live]')).not.toBeNull();
  });
});
