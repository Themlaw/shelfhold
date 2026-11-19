// A stable name for this browser as a reader (L07: `device_id`, 64 characters at most). It lets the
// server tell two devices apart, so that a position written here never silently replaces the one
// another device holds: the further-ahead one is proposed back as a conflict.
const KEY = 'shelfhold.reader.device';

export function readerDeviceId(storage: Pick<Storage, 'getItem' | 'setItem'> | null = safeStorage()): string {
  const known = storage?.getItem(KEY);
  if (known && known.length <= 64) return known;
  const fresh = `web-${globalThis.crypto?.randomUUID?.() ?? Math.random().toString(36).slice(2, 14)}`;
  try {
    storage?.setItem(KEY, fresh);
  } catch {
    // storage refused: this reading has a name, the next one will have another
  }
  return fresh;
}

function safeStorage(): Storage | null {
  try {
    return typeof localStorage === 'undefined' ? null : localStorage;
  } catch {
    return null;
  }
}
