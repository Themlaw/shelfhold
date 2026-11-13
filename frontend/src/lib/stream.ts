// Typed SSE client for `GET /api/v1/events` (EF-INT-002).
//
// The server names each frame by its event type (`event: work.updated`) and seals the `id:` of
// each frame and of each heartbeat: the browser sends the last one back as `Last-Event-ID` when
// it reconnects by itself, and the server replays what came after. When the server cannot trust
// that id (forged, another profile's, from before a purge or a restore, a profile whose role or
// ceiling changed) it sends `resync_required`: the page reloads what it shows, then opens a new
// stream, which starts at the head. The ids are opaque: never read, never compared.

export const STREAM_URL = '/api/v1/events';
export const RESYNC_EVENT = 'resync_required';

export interface StreamEvent {
  type: string;
  visibility: string;
  payload: Record<string, unknown>;
  occurred_at: string;
  title: string | null;
  title_masked: boolean;
}

export interface StreamOptions {
  /** The event types the page listens to (an `EventSource` has no wildcard). */
  types: readonly string[];
  onEvent: (event: StreamEvent) => void;
  /** Reload what the page shows. The stream is reopened when it settles. */
  onResync: () => void | Promise<void>;
  onError?: (error: Event) => void;
  url?: string;
  /** For tests. */
  factory?: (url: string) => EventSource;
}

export interface StreamHandle {
  close: () => void;
}

export function parseStreamEvent(data: string): StreamEvent | null {
  try {
    const parsed: unknown = JSON.parse(data);
    if (typeof parsed === 'object' && parsed !== null && 'type' in parsed && 'payload' in parsed) {
      return parsed as StreamEvent;
    }
  } catch {
    // a frame that is not ours is dropped, not thrown at the page
  }
  return null;
}

export function openEventStream(options: StreamOptions): StreamHandle {
  const url = options.url ?? STREAM_URL;
  const factory = options.factory ?? ((target: string) => new EventSource(target, { withCredentials: true }));
  let source: EventSource | null = null;
  let closed = false;

  const open = (): void => {
    const current = factory(url);
    source = current;
    for (const type of options.types) {
      current.addEventListener(type, (message) => {
        const event = parseStreamEvent((message as MessageEvent<string>).data);
        if (event !== null) options.onEvent(event);
      });
    }
    current.addEventListener(RESYNC_EVENT, () => {
      // A new EventSource carries no `Last-Event-ID`: it starts at the head, after the reload.
      current.close();
      void Promise.resolve(options.onResync()).then(() => {
        if (!closed) open();
      });
    });
    current.onerror = (error) => options.onError?.(error);
  };

  open();
  return {
    close: () => {
      closed = true;
      source?.close();
    },
  };
}
