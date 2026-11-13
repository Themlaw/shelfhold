import { describe, expect, it, vi } from 'vitest';
import { RESYNC_EVENT, openEventStream, parseStreamEvent, type StreamEvent } from './stream';

type Listener = (message: MessageEvent<string>) => void;

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
  emit(name: string, data: string) {
    for (const listener of this.listeners.get(name) ?? []) {
      listener({ data } as MessageEvent<string>);
    }
  }
}

function source(index: number): FakeSource {
  const found = FakeSource.all[index];
  if (found === undefined) throw new Error(`no stream ${index}`);
  return found;
}

const factory = (url: string) => new FakeSource(url) as unknown as EventSource;
const frame: StreamEvent = {
  type: 'work.updated',
  visibility: 'household',
  payload: { work_id: 'w1' },
  occurred_at: '2026-11-05T09:00:00+00:00',
  title: 'A tale',
  title_masked: false,
};

describe('typed event stream', () => {
  // req: EF-INT-002
  it('hands the page each typed frame, parsed', () => {
    FakeSource.all = [];
    const seen: StreamEvent[] = [];
    openEventStream({
      types: ['work.updated'],
      onEvent: (event) => seen.push(event),
      onResync: () => undefined,
      factory,
    });
    source(0).emit('work.updated', JSON.stringify(frame));
    expect(seen).toEqual([frame]);
    expect(source(0).url).toBe('/api/v1/events');
  });

  // req: EF-INT-002
  it('drops a frame that is not ours instead of throwing at the page', () => {
    FakeSource.all = [];
    const onEvent = vi.fn();
    openEventStream({ types: ['work.updated'], onEvent, onResync: () => undefined, factory });
    source(0).emit('work.updated', 'not json');
    source(0).emit('work.updated', '{"x":1}');
    expect(onEvent).not.toHaveBeenCalled();
    expect(parseStreamEvent('[]')).toBeNull();
  });

  // req: EF-INT-002
  it('on resync_required closes the stream, reloads, then reopens a fresh one', async () => {
    FakeSource.all = [];
    let reloaded = false;
    openEventStream({
      types: ['work.updated'],
      onEvent: () => undefined,
      onResync: async () => {
        reloaded = true;
      },
      factory,
    });
    source(0).emit(RESYNC_EVENT, '{}');
    expect(source(0).closed).toBe(true);
    await Promise.resolve();
    await Promise.resolve();
    expect(reloaded).toBe(true);
    expect(FakeSource.all).toHaveLength(2); // a new EventSource: no stale Last-Event-ID
  });

  // req: EF-INT-002
  it('does not reopen after the page closed it', async () => {
    FakeSource.all = [];
    const handle = openEventStream({
      types: [],
      onEvent: () => undefined,
      onResync: async () => undefined,
      factory,
    });
    source(0).emit(RESYNC_EVENT, '{}');
    handle.close();
    await Promise.resolve();
    await Promise.resolve();
    expect(FakeSource.all).toHaveLength(1);
  });
});
