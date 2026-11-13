import { render, screen } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import Page from './+page.svelte';

const BASE = 'http://shelfhold.test';
const server = setupServer();
const prefs = (extra: Record<string, unknown> = {}) => ({
  notify_mode: 'digest', quiet_start: '22:00', quiet_end: '07:00', timezone: 'Europe/Paris',
  effective_quiet: [], ...extra
});

beforeAll(() => {
  server.listen({ onUnhandledRequest: 'error' } as never);
  client.setConfig({ baseUrl: BASE });
});
beforeEach(() => {
  document.cookie = 'csrf=tok123';
});
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe('Préférences de notification', () => {
  // req: EF-USR-016
  it('reads GET /api/v1/me/notifications and replaces the whole of it with PUT', async () => {
    let put: unknown = null;
    server.use(
      http.get(`${BASE}/api/v1/me/notifications`, () => HttpResponse.json(prefs())),
      http.put(`${BASE}/api/v1/me/notifications`, async ({ request }) => {
        put = await request.json();
        return HttpResponse.json(prefs({ notify_mode: 'immediate' }));
      })
    );
    render(Page);
    const user = userEvent.setup();
    expect(((await screen.findByLabelText('Fuseau')) as HTMLInputElement).value).toBe('Europe/Paris');
    await user.selectOptions(screen.getByLabelText('Mode'), 'immediate');
    await user.click(screen.getByRole('button', { name: 'Enregistrer' }));
    expect(await screen.findByText('Enregistré.')).toBeTruthy();
    expect(put).toEqual({
      notify_mode: 'immediate', quiet_start: '22:00', quiet_end: '07:00', timezone: 'Europe/Paris'
    });
  });

  // req: EF-USR-016
  it('sends no quiet hours as null, not as an empty text', async () => {
    let put: Record<string, unknown> = {};
    server.use(
      http.get(`${BASE}/api/v1/me/notifications`, () => HttpResponse.json(prefs())),
      http.put(`${BASE}/api/v1/me/notifications`, async ({ request }) => {
        put = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(prefs({ quiet_start: null, quiet_end: null }));
      })
    );
    render(Page);
    const user = userEvent.setup();
    await user.clear(await screen.findByLabelText('Début de la période calme'));
    await user.clear(screen.getByLabelText('Fin de la période calme'));
    await user.click(screen.getByRole('button', { name: 'Enregistrer' }));
    await screen.findByText('Enregistré.');
    expect(put.quiet_start).toBeNull();
    expect(put.quiet_end).toBeNull();
  });

  // req: EF-USR-016
  it('says what the server refused, from its reason', async () => {
    server.use(
      http.get(`${BASE}/api/v1/me/notifications`, () => HttpResponse.json(prefs())),
      http.put(`${BASE}/api/v1/me/notifications`, () =>
        HttpResponse.json(
          { type: '/api/v1/problems/notification-prefs-refused', title: 'x', status: 422,
            code: 'notification_prefs_refused', reason: 'bad_timezone' },
          { status: 422 }
        )
      )
    );
    render(Page);
    const user = userEvent.setup();
    await screen.findByLabelText('Fuseau');
    await user.click(screen.getByRole('button', { name: 'Enregistrer' }));
    expect((await screen.findByRole('alert')).textContent).toMatch(/refusé/);
  });
});
