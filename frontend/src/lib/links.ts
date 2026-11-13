// The addresses a screen opens come from the server (never from the user's input): the screen
// still opens only a web address, so a value that is anything else (`javascript:`, `data:`) is no
// link at all.
export function webAddress(value: unknown): string | null {
  if (typeof value !== 'string') return null;
  try {
    const url = new URL(value);
    return url.protocol === 'http:' || url.protocol === 'https:' ? value : null;
  } catch {
    return null;
  }
}
