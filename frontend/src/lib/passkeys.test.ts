import { describe, expect, it } from 'vitest';
import { csrfToken, toBytes, toText } from './passkeys.ts';

describe('passkey encoding', () => {
  // req: EF-USR-025
  it('round-trips base64url without padding', () => {
    const bytes = Uint8Array.from([251, 255, 190, 0, 1, 2, 3]);
    const text = toText(bytes.buffer);
    expect(text).toBe('-_--AAECAw');
    expect(Array.from(toBytes(text))).toEqual(Array.from(bytes));
  });

  // req: EF-USR-025
  it('reads the CSRF cookie of either name', () => {
    document.cookie = 'csrf=abc%2B123';
    expect(csrfToken()).toBe('abc+123');
  });
});
