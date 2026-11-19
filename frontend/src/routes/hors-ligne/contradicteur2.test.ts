// PR #396 (M5 L16b), second contradicteur, S4 (🔒, the rest of M6): `/hors-ligne` lists the active
// profile's copies without the session check the reader makes. A SPA that is not reloaded (a tab
// left open, navigated client-side after another profile signed in elsewhere) shows the previous
// profile's titles to whoever holds the device. Red test (`it.fails`) until the page makes the
// reader's check.
import { render, screen } from '@testing-library/svelte';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createOfflineStore, type OfflineStore } from '#lib/offline/store.ts';
import { memoryEnv } from '#lib/offline/testing.ts';
import Page from './+page.svelte';

const held = vi.hoisted(() => ({ store: null as unknown as OfflineStore }));
vi.mock('#lib/offline/runtime.ts', () => ({ offlineStore: () => held.store }));

const ADULT = '01JADULT00000000000000000A';
beforeEach(() => void (held.store = createOfflineStore(memoryEnv())));

describe('second contradicteur (#396): the offline copies page', () => {
  // req: EF-LEC-012, ENF-SEC-010
  it('S4: lists nothing under another session than the one the copies were kept under', async () => {
    await held.store.activate(ADULT);
    await held.store.bindSession('tok-adult');
    await held.store.keep(ADULT, { carrierType: 'content', carrierId: 'C1', workId: 'W1', title: 'Secret', publication: {} as never }, []);
    document.cookie = 'csrf=tok-child'; // another profile signed in; this page was not reloaded
    render(Page);
    await new Promise((r) => setTimeout(r, 50)); // the list is read from the device, in memory here
    expect(screen.queryByText('Secret')).toBeNull(); // today: the previous profile's title is listed
  });
});
