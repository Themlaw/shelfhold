// « Lire hors ligne » on the sheet (M5 L16b, EF-LEC-012).
import { render, screen, waitFor } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { PublicationOut } from '#lib/api/types.gen.ts';
import type { KeepApi } from '#lib/offline/keep.ts';
import { createOfflineStore, type OfflineStore } from '#lib/offline/store.ts';
import { memoryEnv } from '#lib/offline/testing.ts';
import OfflineButton from './OfflineButton.svelte';

const held = vi.hoisted(() => ({ store: null as unknown as OfflineStore, api: null as unknown as KeepApi }));
vi.mock('#lib/offline/runtime.ts', () => ({ offlineStore: () => held.store, offlineApi: () => held.api }));

const ALICE = '01JALICE00000000000000000A';
const publication = (scope: 'full' | 'excerpt' = 'full'): PublicationOut => ({
  carrier_type: 'content', carrier_id: 'C1', work_id: 'W1', series_id: 'W1', scope, kind: 'pages', title: 'Une série', number: null, language: null,
  reading_direction: 'ltr', expires_at: '2026-11-19T10:10:00Z', pages: [{ index: 1, href: '/content/T/p1.jpg', media_type: 'image/jpeg' }], spine: [], package: null
});

function api(over: Partial<KeepApi> = {}): KeepApi {
  return {
    works: async () => ({ status: 200, since: 'tok', removed: [] }),
    publication: async () => ({ status: 200, publication: publication() }),
    putPosition: async () => ({ status: 200 }),
    entry: async () => new Response('x', { headers: { 'content-type': 'image/jpeg' } }),
    ...over
  };
}

beforeEach(() => {
  held.store = createOfflineStore(memoryEnv());
  held.api = api();
});

describe('the offline button', () => {
  // req: EF-LEC-012
  it('keeps the copy for the signed-in profile, says so, and removes it on request', async () => {
    await held.store.activate(ALICE);
    const user = userEvent.setup();
    render(OfflineButton, { unitId: 'U1' });
    await user.click(await screen.findByRole('button', { name: 'Lire hors ligne' }));
    expect(await screen.findByRole('status')).toHaveTextContent('Copie gardée sur cet appareil');
    expect(await held.store.copies(ALICE)).toHaveLength(1);
    expect(screen.getByRole('link', { name: 'Copies hors ligne' })).toHaveAttribute('href', '/hors-ligne');
    await user.click(screen.getByRole('button', { name: 'Retirer la copie' }));
    await waitFor(async () => expect(await held.store.copies(ALICE)).toEqual([]));
    expect(await screen.findByRole('button', { name: 'Lire hors ligne' })).toBeInTheDocument();
  });

  // req: EF-LEC-012
  it('shows a copy already kept when the sheet opens', async () => {
    await held.store.activate(ALICE);
    await held.store.keep(ALICE, { carrierType: 'content', carrierId: 'C1', workId: 'W1', title: 't', via: 'U1', publication: publication() }, []);
    render(OfflineButton, { unitId: 'U1' });
    expect(await screen.findByRole('status')).toHaveTextContent('Copie gardée');
  });

  // req: EF-LEC-012, EF-LEC-014
  it('an extract is refused with a word, and nothing is kept', async () => {
    held.api = api({ publication: async () => ({ status: 200, publication: publication('excerpt') }) });
    await held.store.activate(ALICE);
    const user = userEvent.setup();
    render(OfflineButton, { unitId: 'U1' });
    await user.click(await screen.findByRole('button', { name: 'Lire hors ligne' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('extrait');
    expect(await held.store.copies(ALICE)).toEqual([]);
  });

  // req: EF-LEC-012, EF-USR-006
  it('a book the profile may not read says so; a failed download says to retry', async () => {
    await held.store.activate(ALICE);
    const user = userEvent.setup();
    held.api = api({ publication: async () => ({ status: 404 }) });
    const first = render(OfflineButton, { unitId: 'U1' });
    await user.click(await screen.findByRole('button', { name: 'Lire hors ligne' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('pas lisible par ce profil');
    first.unmount();
    held.api = api({ entry: async () => new Response('no', { status: 500 }) });
    render(OfflineButton, { unitId: 'U1' });
    await user.click(await screen.findByRole('button', { name: 'Lire hors ligne' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('réessaie');
  });

  // req: EF-LEC-012
  it('nobody signed in on this browser: asks to sign in again, keeps nothing', async () => {
    const user = userEvent.setup();
    render(OfflineButton, { unitId: 'U1' });
    await user.click(await screen.findByRole('button', { name: 'Lire hors ligne' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Reconnecte-toi');
  });

  // req: EF-LEC-012
  it('renders nothing without a Unit', () => {
    const { container } = render(OfflineButton, { unitId: null });
    expect(container.querySelector('button')).toBeNull();
  });
});
