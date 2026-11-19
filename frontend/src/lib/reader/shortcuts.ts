// The keyboard of the reader (M5 L13, EF-LEC-011, ENF-A11Y-002): the arrows follow the reading
// direction — never the numbers —, Space and Page Down turn, Home and End reach the first and
// the last page, and ? opens the help. Escape is the page's own: it gives the focus back to the
// controls, so the focus is never trapped.
import { keyAction, type ReadingDirection } from './engine.ts';

export type ReaderShortcut = 'next' | 'previous' | 'first' | 'last' | 'help';

/** What a key does in the reader. The arrows follow the sens (`rtl` turns them around), Home and
 * End answer any direction, and ? answers none (it is never a page). */
export function readerShortcut(direction: ReadingDirection, key: string): ReaderShortcut | null {
  if (key === 'Home') return 'first';
  if (key === 'End') return 'last';
  if (key === '?') return 'help';
  return keyAction(direction, key);
}
