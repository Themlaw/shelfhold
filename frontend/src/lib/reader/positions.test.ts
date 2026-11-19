import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createPositionSender } from './positions.ts';

const page = (index: number) => ({ v: 1 as const, type: 'page' as const, index, count: 10 });

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

describe('the position sender', () => {
  // req: EF-LEC-007
  it('sends only the last position of a burst of page turns', async () => {
    const put = vi.fn(async () => undefined);
    const sender = createPositionSender({
      carrier: { type: 'content', id: 'C1' },
      scope: 'full',
      put,
      delayMs: 800
    });
    sender.push(page(1));
    sender.push(page(2));
    sender.push(page(3));
    expect(put).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(800);
    expect(put).toHaveBeenCalledTimes(1);
    const body = (put.mock.calls[0] as unknown as [Record<string, unknown>])[0];
    expect(body).toMatchObject({
      carrier_type: 'content',
      carrier_id: 'C1',
      locator: page(3),
      scope: 'full'
    });
    expect(typeof body.client_ts).toBe('string');
    expect(Object.keys(body)).not.toContain('user_id');
  });

  // req: EF-LEC-007
  it('names the device when it has a name, so that two devices are never taken for one', async () => {
    const put = vi.fn(async () => undefined);
    const sender = createPositionSender({
      carrier: { type: 'content', id: 'C1' },
      scope: 'full',
      deviceId: 'web-abc',
      put
    });
    sender.push(page(1));
    await sender.flush();
    expect(put.mock.calls[0]).toEqual([expect.objectContaining({ device_id: 'web-abc' })]);
  });

  // req: EF-LEC-007
  it('sends what is waiting when the reader is left', async () => {
    const put = vi.fn(async () => undefined);
    const sender = createPositionSender({ carrier: { type: 'content', id: 'C1' }, scope: 'full', put });
    sender.push(page(4));
    await sender.flush();
    expect(put).toHaveBeenCalledTimes(1);
    await sender.flush();
    expect(put).toHaveBeenCalledTimes(1);
  });

  // req: EF-UX-006
  it('writes nothing at all for an extract', async () => {
    const put = vi.fn(async () => undefined);
    const sender = createPositionSender({ carrier: { type: 'content', id: 'C1' }, scope: 'excerpt', put });
    sender.push(page(2));
    await vi.advanceTimersByTimeAsync(5_000);
    await sender.flush();
    expect(put).not.toHaveBeenCalled();
  });

  // req: EF-LEC-007
  it('does not stop the reading when a write fails', async () => {
    const put = vi.fn(async () => {
      throw new Error('offline');
    });
    const sender = createPositionSender({ carrier: { type: 'content', id: 'C1' }, scope: 'full', put, delayMs: 10 });
    sender.push(page(1));
    await vi.advanceTimersByTimeAsync(10);
    expect(put).toHaveBeenCalledTimes(1);
    sender.push(page(2));
    await vi.advanceTimersByTimeAsync(10);
    expect(put).toHaveBeenCalledTimes(2);
  });
});
