import { render, screen, within } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import Page from './+page.svelte';

const BASE = 'http://shelfhold.test';
const UNIT = '01JUNIT0000000000000000001';
const RARE = '01JUNIT0000000000000000002';
const server = setupServer();
const entry = (unit: string, over: Record<string, unknown> = {}) => ({
  deletion_id: '01JDELE0000000000000000001',
  unit_id: unit,
  level: 'delete_content',
  files: 2,
  bytes: 5_000_000,
  deleted_at: '2026-11-10T10:00:00',
  purge_after: '2026-12-10T10:00:00',
  irreplaceable: false,
  ...over
});
const listing = () =>
  http.get(`${BASE}/api/v1/trash`, () =>
    HttpResponse.json({ entries: [entry(UNIT), entry(RARE, { irreplaceable: true })], total_bytes: 10_000_000 })
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

describe('Corbeille, contradicteur L48 (Opus)', () => {
  // req: EF-BIB-031, EF-BIB-052
  // « Effacer maintenant » on an irreplaceable Unit: the form gives the same warning as for any
  // Unit. The badge sits above, but the moment the word is typed nothing says that no source can
  // bring this one back, which is the very case EF-BIB-031 asks the admin to be told about.
  it('warns, in the erase form itself, that this Unit is irreplaceable', async () => {
    server.use(listing());
    render(Page);
    await screen.findByText(RARE);
    const user = userEvent.setup();
    const row = screen.getByRole('listitem', { name: new RegExp(RARE) });
    await user.click(within(row).getByRole('button', { name: 'Effacer maintenant' }));
    const form = within(row).getByRole('button', { name: 'Effacer pour de bon' }).closest('form');
    expect(form).not.toBeNull();
    expect(form!.textContent).toMatch(/irremplaçable/i);
  });
});
