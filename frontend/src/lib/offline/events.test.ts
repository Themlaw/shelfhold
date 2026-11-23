// What a change of the offline copies tells (M6 L20, EF-LEC-012): the tab that keeps, evicts or
// wipes a copy says so; the others hear it over a BroadcastChannel, and the tab itself directly
// (a channel never delivers to the context that posted).
import { describe, expect, it, vi } from 'vitest';
import type { OfflineChannel } from './events.ts';
import { createOfflineEvents, notifying } from './events.ts';
import { createOfflineStore } from './store.ts';
import { memoryEnv } from './testing.ts';
import { profileKvName } from './env.ts';

const ALICE = '01JALICE00000000000000000A';
const BOB = '01JBOB0000000000000000000B';

/** Two ports of one channel, as two tabs of one browser: a message posted by one is delivered to
 * the others only, as the browser does. */
function tabs(): [OfflineChannel, OfflineChannel] {
  const slots: { hears: ((message: string) => void) | null }[] = [];
  const port = (): OfflineChannel => {
    const slot: { hears: ((message: string) => void) | null } = { hears: null };
    slots.push(slot);
    return {
      hear: (listener) => void (slot.hears = listener),
      close: () => undefined,
      postMessage: (message) => {
        for (const other of slots) if (other !== slot) other.hears?.(message);
      }
    };
  };
  return [port(), port()];
}

describe('a change made in one tab is heard in the others', () => {
  // req: EF-LEC-012
  it('the copies changed in one tab reload the screens of the others, and of the tab itself', async () => {
    const [here, there] = tabs();
    const atHome = createOfflineEvents(here);
    const elsewhere = createOfflineEvents(there);
    const seen: string[] = [];
    atHome.onChanged(() => seen.push('here'));
    elsewhere.onChanged(() => seen.push('there'));

    atHome.changed();

    expect(seen).toEqual(['here', 'there']); // the tab that changed, then the other one
  });

  // req: EF-LEC-012
  it('a listener that stopped hearing is not told again', async () => {
    const [here, there] = tabs();
    const atHome = createOfflineEvents(here);
    const elsewhere = createOfflineEvents(there);
    const heard = vi.fn();
    const stop = elsewhere.onChanged(heard);

    atHome.changed();
    expect(heard).toHaveBeenCalledTimes(1);
    stop();
    atHome.changed();
    expect(heard).toHaveBeenCalledTimes(1);
  });

  // req: EF-LEC-012
  it('a store with no channel still tells its own tab', async () => {
    const bare = createOfflineEvents(null);
    const heard = vi.fn();
    bare.onChanged(heard);
    bare.changed();
    expect(heard).toHaveBeenCalledTimes(1);
  });
});

describe('every change of the copies tells the screens', () => {
  // req: EF-LEC-012
  it('a copy kept, evicted or wiped is a change; reading one is not', async () => {
    const [here] = tabs();
    const events = createOfflineEvents(here);
    const heard = vi.fn();
    events.onChanged(heard);
    const store = notifying(createOfflineStore(memoryEnv()), events);

    await store.activate(ALICE);
    expect(heard).toHaveBeenCalledTimes(1);
    await store.keep(ALICE, { carrierType: 'content', carrierId: 'C1', workId: 'W1', title: 'Tale', publication: {} as never }, []);
    expect(heard).toHaveBeenCalledTimes(2);
    await store.copies(ALICE); // a read is not a change
    expect(heard).toHaveBeenCalledTimes(2);
    await store.evictWorks(ALICE, ['W1']);
    expect(heard).toHaveBeenCalledTimes(3);
    await store.wipeAll();
    expect(heard).toHaveBeenCalledTimes(4);
  });

  // req: EF-LEC-012, ENF-SEC-010
  it('a change that failed still tells: the list is stale either way', async () => {
    const [here] = tabs();
    const events = createOfflineEvents(here);
    const heard = vi.fn();
    events.onChanged(heard);
    const env = memoryEnv();
    const store = notifying(createOfflineStore(env), events);
    await store.activate(ALICE);
    heard.mockClear();
    await expect(store.keep(BOB, { carrierType: 'content', carrierId: 'C1', workId: 'W1', title: 't', publication: {} as never }, [])).rejects.toThrow(
      /not the active profile/
    );
    expect(heard).toHaveBeenCalledTimes(1);
    // nothing of the refused profile is left behind
    expect([...env.memory.kvs.keys()].filter((name) => name === profileKvName(BOB))).toEqual([]);
  });
});
