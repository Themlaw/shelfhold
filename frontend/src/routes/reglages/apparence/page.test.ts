import { render, screen } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import Page from './+page.svelte';

const invalidateAll = vi.hoisted(() => vi.fn(async () => {}));
vi.mock('$app/navigation', () => ({ invalidateAll }));

const BASE = 'http://shelfhold.test';
const server = setupServer();
const prefs = (scheme: string, language: string = 'fr') => ({
  theme: 'bibliotheque', chosen_theme: null, color_scheme: scheme, language
});

beforeAll(() => {
  server.listen({ onUnhandledRequest: 'error' } as never);
  client.setConfig({ baseUrl: BASE });
});
beforeEach(() => {
  document.cookie = 'csrf=tok123';
  invalidateAll.mockClear();
});
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe('Appearance screen', () => {
  // req: EF-UX-012
  it('saves the color scheme, then has the root layout read it again', async () => {
    let patched: unknown = null;
    server.use(
      http.get(`${BASE}/api/v1/me/preferences`, () => HttpResponse.json(prefs('auto'))),
      http.patch(`${BASE}/api/v1/me/preferences`, async ({ request }) => {
        patched = await request.json();
        return HttpResponse.json(prefs('dark'));
      })
    );
    render(Page);
    const user = userEvent.setup();
    await user.selectOptions(await screen.findByLabelText('Schéma de couleurs'), 'Sombre');
    await user.click(screen.getByRole('button', { name: 'Enregistrer' }));
    expect(await screen.findByText('Enregistré.')).toBeTruthy();
    expect(patched).toEqual({ color_scheme: 'dark' });
    expect(invalidateAll).toHaveBeenCalledOnce(); // the layout applies it: no screen touches the theme
  });

  // req: ENF-I18N-001
  it('saves the interface language and applies it at once', async () => {
    let patched: unknown = null;
    server.use(
      http.get(`${BASE}/api/v1/me/preferences`, () => HttpResponse.json(prefs('auto'))),
      http.patch(`${BASE}/api/v1/me/preferences`, async ({ request }) => {
        patched = await request.json();
        return HttpResponse.json(prefs('auto', 'en'));
      })
    );
    render(Page);
    await userEvent.setup().selectOptions(await screen.findByLabelText("Langue de l'interface"), 'English');
    expect(patched).toEqual({ language: 'en' });
    expect(invalidateAll).toHaveBeenCalledOnce(); // the layout reads the preference again
  });
});
