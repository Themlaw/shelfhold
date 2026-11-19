import { describe, expect, it } from 'vitest';
import { readerDeviceId } from './device.ts';

const memory = (initial: Record<string, string> = {}) => {
  const data = { ...initial };
  return {
    data,
    getItem: (k: string) => data[k] ?? null,
    setItem: (k: string, v: string) => void (data[k] = v)
  };
};

describe('the device name of the reader', () => {
  // req: EF-LEC-007
  it('is made once, kept, and short enough for the server', () => {
    const storage = memory();
    const first = readerDeviceId(storage);
    expect(first.length).toBeLessThanOrEqual(64);
    expect(first.startsWith('web-')).toBe(true);
    expect(readerDeviceId(storage)).toBe(first);
  });

  // req: EF-LEC-007
  it('is replaced when what was kept is too long to be sent', () => {
    const storage = memory({ 'shelfhold.reader.device': 'x'.repeat(80) });
    expect(readerDeviceId(storage).length).toBeLessThanOrEqual(64);
  });

  // req: EF-LEC-007
  it('still gives a name when the storage refuses', () => {
    const refusing = {
      getItem: () => null,
      setItem: () => {
        throw new Error('quota');
      }
    };
    expect(readerDeviceId(refusing).length).toBeGreaterThan(4);
    expect(readerDeviceId(null).length).toBeGreaterThan(4);
  });
});
