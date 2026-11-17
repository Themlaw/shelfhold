import { describe, expect, it } from 'vitest';
import { formatBytes } from './bytes.ts';

describe('formatBytes', () => {
  // req: EF-INT-020
  it('steps by 1024 and keeps one decimal at most', () => {
    expect(formatBytes(0)).toMatch(/^0\s*o$/);
    expect(formatBytes(1536)).toMatch(/^1,5\s*ko$/i);
    expect(formatBytes(157_286_400)).toMatch(/^150\s*Mo$/);
    expect(formatBytes(5 * 1024 ** 4)).toMatch(/^5\s*To$/);
  });

  // req: EF-INT-020
  it('never goes negative', () => {
    expect(formatBytes(-5)).toMatch(/^0\s*o$/);
  });
});
