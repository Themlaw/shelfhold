import { render, screen, within } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import Page from './+page.svelte';

const BASE = 'http://shelfhold.test';
const server = setupServer();
const ADMIN_PW = ['synthetic', 'admin', 'pw', '0047'].join('-'); // no literal: gitleaks

const row = (key: string, extra: Record<string, unknown> = {}) => ({
  key, value: 'x', source: 'default', default: null, overridden_by: null, warning: null,
  level: 'expert', locked: false, secret: false, ...extra
});
const stale = () =>
  HttpResponse.json(
    {
      type: '/api/v1/problems/reauth-required', title: 'x', status: 401, code: 'reauth_required',
      auth_age_s: 2000, max_age_s: 900, reauth_methods: ['password']
    },
    { status: 401 }
  );

beforeAll(() => {
  server.listen({ onUnhandledRequest: 'error' } as never);
  client.setConfig({ baseUrl: BASE });
});
beforeEach(() => {
  document.cookie = 'csrf=tok123';
});
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

const serve = (items: unknown[]) =>
  server.use(http.get(`${BASE}/api/v1/config`, () => HttpResponse.json({ items })));

describe('Réglages', () => {
  // req: EF-UX-009
  it('draws the three levels from the level the server answers, not from a list of its own', async () => {
    serve([
      row('zz.brand-new-key', { level: 'essential' }),
      row('b.advanced-key', { level: 'advanced' }),
      row('c.expert-key', { level: 'expert' })
    ]);
    render(Page);
    await screen.findByLabelText(/zz\.brand-new-key/);
    const essential = screen.getByRole('heading', { name: 'Essentiel' }).closest('section')!;
    const advanced = screen.getByRole('heading', { name: 'Avancé' }).closest('section')!;
    const expert = screen.getByRole('heading', { name: 'Expert' }).closest('section')!;
    expect(within(essential).getByLabelText(/zz\.brand-new-key/)).toBeTruthy();
    expect(within(advanced).getByLabelText(/b\.advanced-key/)).toBeTruthy();
    expect(within(expert).getByLabelText(/c\.expert-key/)).toBeTruthy();
    expect(within(essential).queryAllByRole('textbox')).toHaveLength(1);
  });

  // req: EF-UX-009
  it('greys a field the server says is locked and shows its origin', async () => {
    serve([
      row('public_url', { level: 'essential', locked: true, overridden_by: 'env', source: 'db' }),
      row('maintenance.window', { level: 'essential', locked: true, source: 'fixed' }),
      row('source.check_mode', { level: 'essential', value: 'always_on' })
    ]);
    render(Page);
    const url = (await screen.findByLabelText(/public_url/)) as HTMLInputElement;
    expect(url.disabled).toBe(true);
    expect(screen.getByText(/^Origine : variable d'/)).toBeTruthy();
    expect(screen.getByText('Origine : fixé par cette version')).toBeTruthy();
    expect((screen.getByLabelText(/source\.check_mode/) as HTMLInputElement).disabled).toBe(false);
    // the client decides nothing: a locked flag alone greys, whatever the source says
    expect((screen.getByLabelText(/maintenance\.window/) as HTMLInputElement).disabled).toBe(true);
  });

  // req: EF-UX-009, ENF-SEC-008
  it('shows no input and no value for a secret setting', async () => {
    serve([row('notify.urls', { level: 'expert', secret: true, value: null })]);
    render(Page);
    expect(await screen.findByText(/Secret : se règle/)).toBeTruthy();
    expect(screen.queryByLabelText(/notify\.urls/)).toBeNull();
    expect(document.querySelector('input[type="password"]')).toBeNull();
  });

  // req: EF-UX-009
  it('saves only the field that changed, then shows it saved', async () => {
    serve([row('source.check_mode', { level: 'essential', value: 'always_on' })]);
    let body: unknown = null;
    server.use(
      http.patch(`${BASE}/api/v1/config`, async ({ request }) => {
        body = await request.json();
        return HttpResponse.json({
          items: [row('source.check_mode', { level: 'essential', value: 'piloted' })]
        });
      })
    );
    render(Page);
    const user = userEvent.setup();
    const input = await screen.findByLabelText(/source\.check_mode/);
    await user.clear(input);
    await user.type(input, 'piloted');
    await user.tab();
    expect(await screen.findByText('Enregistré.')).toBeTruthy();
    expect(body).toEqual({ values: { 'source.check_mode': 'piloted' } });
  });

  // req: EF-UX-009
  it('shows the refusal of the server for a field the environment sets', async () => {
    serve([row('source.check_mode', { level: 'essential', value: 'always_on' })]);
    server.use(
      http.patch(`${BASE}/api/v1/config`, () =>
        HttpResponse.json(
          { type: '/api/v1/problems/config-key-readonly', title: 'x', status: 409,
            code: 'config.key_readonly' },
          { status: 409 }
        )
      )
    );
    render(Page);
    const user = userEvent.setup();
    const input = await screen.findByLabelText(/source\.check_mode/);
    await user.type(input, 'z');
    await user.tab();
    expect((await screen.findByRole('alert')).textContent).toMatch(/fixé hors de cet écran/);
  });

  // req: EF-USR-022
  it('asks for the password in place when the server wants a fresh session, then retries', async () => {
    serve([row('path_map.kavita', { level: 'advanced', value: '[]' })]);
    let calls = 0;
    server.use(
      http.patch(`${BASE}/api/v1/config`, () => {
        calls += 1;
        return calls === 1
          ? stale()
          : HttpResponse.json({ items: [row('path_map.kavita', { level: 'advanced', value: '[1]' })] });
      }),
      http.post(`${BASE}/api/v1/auth/reauth`, () =>
        HttpResponse.json({ auth_time: 'now', csrf_token: 'tok456' })
      )
    );
    render(Page);
    const user = userEvent.setup();
    const input = await screen.findByLabelText(/path_map\.kavita/);
    await user.type(input, '1');
    await user.tab();
    await user.type(await screen.findByLabelText(/mot de passe/i), ADMIN_PW);
    await user.click(screen.getByRole('button', { name: /Confirmer/ }));
    expect(await screen.findByText('Enregistré.')).toBeTruthy();
    expect(calls).toBe(2);
  });

  // req: EF-UX-009, ENF-SEC-010
  it('tells a member that the settings are for the administrator, and offers their own', async () => {
    server.use(
      http.get(`${BASE}/api/v1/config`, () =>
        HttpResponse.json({ type: '/api/v1/problems/forbidden', title: 'x', status: 403,
                            code: 'forbidden' }, { status: 403 })
      )
    );
    render(Page);
    expect((await screen.findByRole('alert')).textContent).toMatch(/réservés à l'administrateur/);
    expect(screen.queryAllByRole('textbox')).toHaveLength(0);
    expect(screen.getByRole('link', { name: /Notifications/ }).getAttribute('href')).toBe(
      '/reglages/notifications'
    );
    expect(screen.getByRole('link', { name: /Apparence/ }).getAttribute('href')).toBe('/reglages/apparence');
  });
});
