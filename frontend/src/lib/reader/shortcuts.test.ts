import { describe, expect, it } from 'vitest';
import { readerShortcut } from './shortcuts.ts';

describe('the keyboard of the reader', () => {
  // req: EF-LEC-011
  it('follows the reading direction with the arrows, never the numbers', () => {
    expect(readerShortcut('ltr', 'ArrowRight')).toBe('next');
    expect(readerShortcut('ltr', 'ArrowLeft')).toBe('previous');
    expect(readerShortcut('rtl', 'ArrowLeft')).toBe('next');
    expect(readerShortcut('rtl', 'ArrowRight')).toBe('previous');
  });

  // req: EF-LEC-011
  it('turns with Space, Page Down and Page Up', () => {
    for (const key of [' ', 'PageDown']) expect(readerShortcut('ltr', key)).toBe('next');
    expect(readerShortcut('ltr', 'PageUp')).toBe('previous');
  });

  // req: EF-LEC-011
  it('reaches the first and the last page with Home and End, in any direction', () => {
    for (const direction of ['ltr', 'rtl', 'webtoon'] as const) {
      expect(readerShortcut(direction, 'Home')).toBe('first');
      expect(readerShortcut(direction, 'End')).toBe('last');
    }
  });

  // req: EF-LEC-011
  it('opens the help with ?', () => {
    expect(readerShortcut('ltr', '?')).toBe('help');
    expect(readerShortcut('webtoon', '?')).toBe('help');
  });

  it('says nothing of a key that does nothing', () => {
    for (const key of ['a', 'Enter', 'Shift', 'F1', 'é']) {
      expect(readerShortcut('ltr', key)).toBeNull();
    }
  });
});
