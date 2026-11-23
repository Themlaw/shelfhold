// The copies kept on this device (M5 L16b, EF-LEC-012; M6 L20: the list follows the changes).
import { render, screen } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createOfflineStore, type OfflineStore } from '#lib/offline/store.ts';
import { memoryEnv } from '#lib/offline/testing.ts';
import Page from './+page.svelte';

const held = vi.hoisted(() => ({ store: null as unknown as OfflineStore, listener: null as (() => void) | null }));
vi.mock('#lib/offline/runtime.ts', () => ({
  offlineStore: () => held.store,
  // what the page hears in production: a copy kept, evicted or wiped, in this tab or in another
  onOfflineChanged: (listener: () => void) => {
    held.listener = listener;
    return () => undefined;
  }
}));

const ALICE = '01JALICE00000000000000000A';
const BOB = '01JBOB0000000000000000000B';
const COPY = { carrierType: 'content', carrierId: 'C1', workId: 'W1', title: '<b>Tale</b>', publication: {} as never };

beforeEach(() => {
  held.store = createOfflineStore(memoryEnv());
  held.listener = null;
});

describe('the offline copies page', () => {
  // req: EF-LEC-012
  it('lists the signed-in profile copies as text, links to the reader, and removes one', async () => {
    await held.store.activate(ALICE);
    await held.store.keep(ALICE, COPY, []);
    const user = userEvent.setup();
    render(Page);
    expect(await screen.findByText('<b>Tale</b>')).toBeInTheDocument(); // text, never markup
    expect(screen.getByRole('link', { name: 'Lire' })).toHaveAttribute('href', '/lire/content/C1');
    await user.click(screen.getByRole('button', { name: 'Retirer la copie' }));
    expect(await screen.findByText('Aucune copie gardée sur cet appareil.')).toBeInTheDocument();
    expect(await held.store.copies(ALICE)).toEqual([]);
  });

  // req: EF-LEC-012, ENF-SEC-010
  it('shows nothing of a profile that is not the active one', async () => {
    await held.store.activate(ALICE);
    await held.store.keep(ALICE, COPY, []);
    await held.store.activate(BOB);
    render(Page);
    expect(await screen.findByText('Aucune copie gardée sur cet appareil.')).toBeInTheDocument();
    expect(screen.queryByText('<b>Tale</b>')).toBeNull();
  });

  // req: EF-LEC-012, EF-INT-001
  it('offline list refreshes after eviction: a Work that left the view goes without a reload', async () => {
    await held.store.activate(ALICE);
    await held.store.keep(ALICE, COPY, []);
    render(Page);
    expect(await screen.findByText('<b>Tale</b>')).toBeInTheDocument();
    expect(held.listener).toBeTruthy(); // the page listens from the moment it opens
    // the sync evicted the copy: `?since=` named the Work (another tab, or this one)
    await held.store.evictWorks(ALICE, ['W1']);
    expect(screen.getByText('<b>Tale</b>')).toBeInTheDocument(); // not yet told
    held.listener!();
    expect(await screen.findByText('Aucune copie gardée sur cet appareil.')).toBeInTheDocument();
  });
});
