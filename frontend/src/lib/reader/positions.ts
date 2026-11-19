// Sending the position to the server (L07): the last place of a burst of page turns, after a short
// quiet, and what is waiting when the reader is left. An extract writes nothing, ever (D-P1): the
// guard is here, and the server refuses it too.
import type { PositionIn } from '#lib/api/types.gen.ts';
import type { Locator, Scope } from './engine.ts';

export const POSITION_DELAY_MS = 800;

export interface Carrier {
  type: 'content' | 'export_item';
  id: string;
}

export interface PositionSender {
  push(locator: Locator): void;
  flush(): Promise<void>;
  /** Drops what is waiting, without sending it. */
  cancel(): void;
}

export function createPositionSender(options: {
  carrier: Carrier;
  scope: Scope;
  put: (body: PositionIn) => Promise<unknown>;
  deviceId?: string;
  delayMs?: number;
  now?: () => Date;
}): PositionSender {
  const { carrier, scope, put } = options;
  const delay = options.delayMs ?? POSITION_DELAY_MS;
  const now = options.now ?? (() => new Date());
  let waiting: Locator | null = null;
  let timer: ReturnType<typeof setTimeout> | null = null;

  const stop = () => {
    if (timer !== null) clearTimeout(timer);
    timer = null;
  };

  async function send(): Promise<void> {
    stop();
    const locator = waiting;
    waiting = null;
    if (locator === null) return;
    try {
      await put({
        carrier_type: carrier.type,
        carrier_id: carrier.id,
        client_ts: now().toISOString(),
        locator: { ...locator },
        scope,
        ...(options.deviceId ? { device_id: options.deviceId } : {})
      });
    } catch {
      // the next page turn says where the reader is again: a write lost is not a reading stopped
    }
  }

  return {
    push(locator) {
      if (scope === 'excerpt') return;
      waiting = locator;
      stop();
      timer = setTimeout(() => void send(), delay);
    },
    flush: send,
    cancel() {
      stop();
      waiting = null;
    }
  };
}
