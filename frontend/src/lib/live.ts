// "Mise à jour en direct" (M3 L41): a screen reloads what it shows when the one typed stream of
// L12 (`GET /api/v1/events`) says that something it may see has arrived. No second door, no
// polling: the stream is the server's, so what it carries is already filtered for the reader, and
// a frame is only a reason to ask the (filtered) list again.
import { openEventStream } from '#lib/stream.ts';

/** The event types that change what the home and activity screens show. */
export const LIVE_TYPES = ['ops.request_arrived', 'unit.stored'] as const;

/** Reload on every such frame, and again after a `resync_required`. Returns what closes it. */
export function reloadOnStream(reload: () => void | Promise<void>): () => void {
  const handle = openEventStream({
    types: LIVE_TYPES,
    onEvent: () => void reload(),
    onResync: reload
  });
  return () => handle.close();
}
