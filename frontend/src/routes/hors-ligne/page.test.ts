// The copies kept on this device (M5 L16b, EF-LEC-012).
import { render, screen } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createOfflineStore, type OfflineStore } from '#lib/offline/store.ts';
import { memoryEnv } from '#lib/offline/testing.ts';
import Page from './+page.svelte';

const held = vi.hoisted(() => ({ store: null as unknown as OfflineStore }));
vi.mock('#lib/offline/runtime.ts', () => ({ offlineStore: () => held.store }));

const ALICE = '01JALICE00000000000000000A';
beforeEach(() => void (held.store = createOfflineStore(memoryEnv())));

describe('the offline copies page', () => {
  // req: EF-LEC-012
  it('lists the signed-in profile copies as text, links to the reader, and removes one', async () => {
    await held.store.activate(ALICE);
    await held.store.keep(ALICE, { carrierType: 'content', carrierId: 'C1', workId: 'W1', title: '<b>Tale</b>', publication: {} as never }, []);
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
    await held.store.keep(ALICE, { carrierType: 'content', carrierId: 'C1', workId: 'W1', title: 'Secret', publication: {} as never }, []);
    await held.store.activate('01JBOB0000000000000000000B');
    render(Page);
    expect(await screen.findByText('Aucune copie gardée sur cet appareil.')).toBeInTheDocument();
    expect(screen.queryByText('Secret')).toBeNull();
  });
});
