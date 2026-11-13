import { render, screen, waitFor } from '@testing-library/svelte';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { client } from '#lib/api/client.gen.ts';
import Page from './+page.svelte';

const BASE = 'http://shelfhold.test';
type Listener = (message: MessageEvent<string>) => void;
const FRAME = JSON.stringify({
  type: 'ops.request_arrived',
  visibility: 'household',
  payload: {},
  occurred_at: '2026-11-03T09:00:00Z',
  title: null,
  title_masked: false
});

class FakeSource {
  static all: FakeSource[] = [];
  listeners = new Map<string, Listener[]>();
  closed = false;
  onerror: ((event: Event) => void) | null = null;
  constructor(public url: string) {
    FakeSource.all.push(this);
  }
  addEventListener(name: string, listener: Listener) {
    this.listeners.set(name, [...(this.listeners.get(name) ?? []), listener]);
  }
  close() {
    this.closed = true;
  }
  emit(name: string) {
    for (const listener of this.listeners.get(name) ?? []) listener({ data: FRAME } as MessageEvent<string>);
  }
}

let feed: unknown[] = [];
const server = setupServer(http.get(`${BASE}/api/v1/activity`, () => HttpResponse.json(feed)));
beforeAll(() => {
  server.listen({ onUnhandledRequest: 'error' } as never);
  client.setConfig({ baseUrl: BASE });
});
beforeEach(() => {
  feed = [];
  FakeSource.all = [];
  vi.stubGlobal('EventSource', FakeSource);
});
afterEach(() => {
  server.resetHandlers();
  vi.unstubAllGlobals();
});
afterAll(() => server.close());

const entry = (id: number, over: Record<string, unknown> = {}) => ({
  event_id: id,
  type: 'ops.request_arrived',
  visibility: 'household',
  payload: {},
  occurred_at: '2026-11-03T09:00:00Z',
  title: `Titre ${id}`,
  title_masked: false,
  family_state: 'available',
  open_url: 'http://kavita.test:5000',
  koreader_reset: false,
  ...over
});

describe('activity page', () => {
  // req: EF-UX-008
  it('test_empty_states_action_oriented_activity', async () => {
    render(Page);
    expect(await screen.findByText('Rien ne s’est passé pour l’instant.')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Chercher un livre' })).toHaveAttribute('href', '/chercher');
  });

  // req: EF-UX-010
  it('test_activity_lists_items_and_refreshes_on_the_stream', async () => {
    feed = [entry(1)];
    render(Page);
    expect(await screen.findByText('Titre 1')).toBeInTheDocument();
    expect(FakeSource.all.map((s) => s.url)).toEqual(['/api/v1/events']);
    feed = [entry(2), entry(1)];
    FakeSource.all[0]!.emit('ops.request_arrived');
    await waitFor(() => expect(screen.getByText('Titre 2')).toBeInTheDocument());
  });

  // req: EF-UX-010
  it('test_activity_a_server_refusal_is_a_card_not_a_crash', async () => {
    server.use(
      http.get(`${BASE}/api/v1/activity`, () =>
        HttpResponse.json(
          { type: '/api/v1/problems/x', title: 'x', status: 403, code: 'forbidden' },
          { status: 403 }
        )
      )
    );
    render(Page);
    expect(await screen.findByRole('alert')).toBeInTheDocument();
  });
});
