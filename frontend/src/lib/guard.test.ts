import { describe, expect, it, vi } from 'vitest';
import { Reauth } from './guard.svelte.ts';

const STALE = { status: 401, code: 'reauth-required', type: 'reauth-required', reauth_methods: ['passkey'] };

describe('Reauth', () => {
  // req: EF-USR-022
  it('asks again for a stale password, then replays the action once', async () => {
    const reauth = new Reauth();
    const replay = vi.fn(async () => {});
    expect(reauth.askAgain(STALE, replay)).toBe(true);
    expect(reauth.reauthMethods).toEqual(['passkey']);
    await reauth.afterReauth();
    expect(replay).toHaveBeenCalledTimes(1);
    expect(reauth.reauthMethods).toBeNull();
    await reauth.afterReauth();
    expect(replay).toHaveBeenCalledTimes(1);
  });

  // req: EF-USR-022
  it('falls back to the password and drops the action on cancel', async () => {
    const reauth = new Reauth();
    const replay = vi.fn(async () => {});
    reauth.askAgain({ ...STALE, reauth_methods: undefined }, replay);
    expect(reauth.reauthMethods).toEqual(['password']);
    reauth.cancelReauth();
    await reauth.afterReauth();
    expect(reauth.reauthMethods).toBeNull();
    expect(replay).not.toHaveBeenCalled();
  });

  // req: EF-USR-022
  it('leaves any other answer to the screen', () => {
    const reauth = new Reauth();
    expect(reauth.askAgain({ status: 403 }, async () => {})).toBe(false);
    expect(reauth.reauthMethods).toBeNull();
  });
});
