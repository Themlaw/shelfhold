// @vitest-environment node
// The worker serves the copies of the active profile, and only them (M5 L16b, EF-LEC-012; ADR-0055
// « Crédential »): `/content/*` stays out of the shell cache, but the copy of a book the profile
// chose to keep is answered, with the headers it was served with, with the network cut.
import { describe, expect, it, vi } from 'vitest';
import { copyUrl } from './env.ts';
import { createOfflineStore } from './store.ts';
import { memoryEnv } from './testing.ts';
import { installPwa, type PwaScope } from '#lib/pwa/worker.ts';

const ORIGIN = 'https://shelfhold.example';
const ALICE = '01JALICE00000000000000000A';
const BOB = '01JBOB0000000000000000000B';
const CARRIER = '01JCARRIER000000000000000C';

async function bench() {
  const env = memoryEnv();
  const store = createOfflineStore(env);
  const handlers: Record<string, (event: Record<string, unknown>) => void> = {};
  const fetchNet = vi.fn(async () => { throw new TypeError('Failed to fetch'); });
  const scope = {
    location: { origin: ORIGIN }, caches: env.caches, fetch: fetchNet,
    addEventListener: (type: string, handler: (event: Record<string, unknown>) => void) => void (handlers[type] = handler),
    skipWaiting: async () => undefined, clients: { claim: async () => undefined }
  };
  installPwa(scope as unknown as PwaScope, [{ url: 'index.html', revision: 'r' }], 'v1', env);
  const ask = async (path: string, method = 'GET') => {
    const event: Record<string, unknown> & { answer?: Promise<Response> } = {
      request: { url: `${ORIGIN}${path}`, method, mode: 'cors' },
      respondWith: (p: Promise<Response>) => void (event.answer = p)
    };
    handlers['fetch']?.(event);
    return event.answer ? await event.answer : null;
  };
  return { env, store, ask, fetchNet };
}

async function keep(store: ReturnType<typeof createOfflineStore>, profile: string) {
  await store.activate(profile);
  await store.keep(profile, { carrierType: 'content', carrierId: CARRIER, workId: 'W', title: 't', publication: {} as never }, [
    { url: copyUrl('content', CARRIER, 'p1.png'), response: new Response('PNG', { headers: { 'content-type': 'image/png', 'content-security-policy': 'sandbox' } }) }
  ]);
}

describe('the worker serves an offline copy', () => {
  // req: EF-LEC-012
  it('test_pwa_offline_cache: a kept book reads with the network cut, headers kept, and the network never asked', async () => {
    const { store, ask, fetchNet } = await bench();
    await keep(store, ALICE);
    const answer = await ask(copyUrl('content', CARRIER, 'p1.png'));
    expect(answer!.status).toBe(200);
    expect(await answer!.text()).toBe('PNG');
    expect(answer!.headers.get('content-security-policy')).toBe('sandbox');
    expect(fetchNet).not.toHaveBeenCalled();
  });

  // req: EF-LEC-012, ENF-SEC-010
  it('offline copies are not shared between profiles: once another profile is active, nothing is served', async () => {
    const { store, ask, fetchNet } = await bench();
    await keep(store, ALICE);
    await store.activate(BOB);
    expect((await ask(copyUrl('content', CARRIER, 'p1.png')))!.status).toBe(404);
    expect(fetchNet).not.toHaveBeenCalled(); // a miss is a 404, never a try at the network
  });

  // req: EF-LEC-012, ENF-SEC-010
  it('every other /content request is left to the network, and a POST or an odd path is never answered', async () => {
    const { store, ask } = await bench();
    await keep(store, ALICE);
    expect(await ask('/content/TICKETVALUE/p1.png')).toBeNull();
    expect(await ask(copyUrl('content', CARRIER, 'p1.png'), 'POST')).toBeNull();
    expect(await ask(`/content/offline.content.${CARRIER}/..%2fp1.png`)).toBeNull();
    expect(await ask(`/content/offline.content.${CARRIER}//p1.png`)).toBeNull();
  });
});
