import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { render, screen } from '@testing-library/svelte';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import Page from './+page.svelte';

const BASE = 'http://shelfhold.test';
const server = setupServer();
const ENTRY = {
  targets: [
    {
      target_id: '01JTARG0000000000000000001',
      name: 'source-a',
      kover: 'https://kavita.example',
      opds: 'https://kavita.example/api/opds',
      kosync: 'https://kavita.example/api/koreader'
    }
  ]
};
const serve = (body: unknown, status = 200) =>
  http.get(`${BASE}/api/v1/devices/connect`, () => HttpResponse.json(body as never, { status }));

beforeAll(() => {
  // any request but the one list fails the test: the QR is drawn here, with no network
  server.listen({ onUnhandledRequest: 'error' } as never);
  client.setConfig({ baseUrl: BASE });
});
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe('Connecter un appareil', () => {
  // req: EF-UX-004
  it('test_connect_device_qr_kavita_endpoints', async () => {
    server.use(serve(ENTRY));
    const { container } = render(Page);
    expect(await screen.findByRole('heading', { name: 'Connecter un appareil' })).toBeInTheDocument();
    await screen.findAllByRole('img');
    const codes = [...container.querySelectorAll('svg[data-qr-text]')];
    expect(codes.map((c) => c.getAttribute('data-qr-text'))).toEqual([
      'https://kavita.example',
      'https://kavita.example/api/koreader',
      'https://kavita.example/api/opds',
      'https://kavita.example/api/opds'
    ]);
    for (const code of codes) expect(code).toHaveAccessibleName();
    // each address is also written, for whoever cannot scan
    expect(screen.getAllByText('https://kavita.example/api/opds')).toHaveLength(2);
    expect(screen.getByText(/Kover/)).toBeInTheDocument();
    expect(screen.getAllByText(/KOReader/)).toHaveLength(2);
    expect(screen.getByText(/liseuse/i)).toBeInTheDocument();
  });

  // req: EF-UX-004, ENF-SEC-010
  it('puts no token and no key in the page or in a code, and says where the key is made', async () => {
    server.use(serve(ENTRY));
    const { container } = render(Page);
    await screen.findAllByRole('img');
    const text = container.innerHTML.toLowerCase();
    for (const word of ['apikey', 'api_key', 'token', 'secret', 'password', 'shk_', '@']) {
      expect(text).not.toContain(word);
    }
    for (const code of container.querySelectorAll('svg[data-qr-text]')) {
      expect(code.getAttribute('data-qr-text')).not.toMatch(/[?#@]/);
    }
    // the key of the account stays in Kavita: the screen says so, it never fetches it
    expect(screen.getByText(/clé .* dans Kavita/i)).toBeInTheDocument();
  });

  // req: EF-UX-004
  it('draws no code for an address that is not a web address', async () => {
    server.use(
      serve({
        targets: [{ ...ENTRY.targets[0], kover: 'javascript:alert(1)', opds: 'data:text/html,x' }]
      })
    );
    const { container } = render(Page);
    await screen.findAllByRole('img');
    const drawn = [...container.querySelectorAll('svg[data-qr-text]')].map((c) =>
      c.getAttribute('data-qr-text')
    );
    expect(drawn).toEqual(['https://kavita.example/api/koreader']);
    expect(container.innerHTML).not.toContain('javascript:');
  });

  // req: EF-UX-004, EF-USR-006
  it('is closed to a member and a child, and without a session', async () => {
    server.use(serve({ type: '/api/v1/problems/forbidden', title: 'x', status: 403, code: 'forbidden' }, 403));
    const { container } = render(Page);
    expect(await screen.findByRole('alert')).toBeInTheDocument();
    expect(container.querySelector('svg')).toBeNull();
    server.use(serve({ type: '/api/v1/problems/x', title: 'x', status: 401, code: 'x' }, 401));
    const again = render(Page);
    expect(await again.findAllByRole('alert')).not.toHaveLength(0);
  });

  // req: EF-UX-004
  it('the code is drawn locally: the generator source holds no network call', () => {
    const source = readFileSync(join(process.cwd(), 'src/lib/components/QrCode.svelte'), 'utf8');
    for (const word of ['fetch(', 'XMLHttpRequest', 'WebSocket', 'sendBeacon', 'http://', 'https://']) {
      expect(source).not.toContain(word);
    }
  });
});
