// jsdom ships no types and is a test dependency only: this is the part the reader's tests use.
declare module 'jsdom' {
  export class VirtualConsole {}
  export class JSDOM {
    constructor(html: string, options?: Record<string, unknown>);
    window: Window & typeof globalThis;
  }
}
