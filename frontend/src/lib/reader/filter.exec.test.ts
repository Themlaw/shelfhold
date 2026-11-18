// The filter alone, with no policy at all (an EPUB `publisher` whose CSP was taken away): what it
// returns must not run a script in a document that has no CSP, whatever the vector. The control is
// the same document unfiltered, which does run: the harness would see it.
import { JSDOM, VirtualConsole } from 'jsdom';
import { describe, expect, it } from 'vitest';
import { filterMarkup } from './filter.ts';

const OPTS = { origin: 'https://shelfhold.test' };
const NS = 'xmlns="http://www.w3.org/1999/xhtml"';
const run = (xhtml: string, contentType = 'application/xhtml+xml') => {
  const dom = new JSDOM(xhtml, { contentType, runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: new VirtualConsole() });
  return dom.window as unknown as Window & typeof globalThis & { __ran?: number };
};
const body = (inner: string) => `<html ${NS}><head><title>t</title></head><body>${inner}</body></html>`;

// Each vector: the document, and the events a reader (or the loader) would raise on it. jsdom loads
// nothing, so an event that a resource would raise is raised by hand.
const vectors: Record<string, { doc: string; fire: [string, string][] }> = {
  'inline script': { doc: body('<script>window.__ran=1</script>'), fire: [] },
  'script in the head': { doc: `<html ${NS}><head><title>t</title><script>window.__ran=1</script></head><body/></html>`, fire: [] },
  'on* on an image': { doc: body('<img id="x" src="blob:x" onerror="window.__ran=1" onload="window.__ran=1"/>'), fire: [['#x', 'error'], ['#x', 'load']] },
  'on* on a paragraph': { doc: body('<p id="x" onclick="window.__ran=1" onmouseover="window.__ran=1">t</p>'), fire: [['#x', 'click'], ['#x', 'mouseover']] },
  'svg on*': { doc: body('<svg xmlns="http://www.w3.org/2000/svg"><rect id="x" onclick="window.__ran=1" width="1" height="1"/></svg>'), fire: [['#x', 'click']] }
};

describe('the filter alone, with no policy', () => {
  // req: ENF-SEC-004
  for (const [name, { doc, fire }] of Object.entries(vectors)) {
    it(`test_epub_script_never_runs (filter alone): ${name}`, () => {
      const type = doc.startsWith('<svg') ? 'image/svg+xml' : 'application/xhtml+xml';
      const raise = (w: Window & typeof globalThis) => {
        for (const [selector, kind] of fire) w.document.querySelector(selector)?.dispatchEvent(new w.Event(kind, { bubbles: true }));
      };
      const control = run(doc, type);
      raise(control);
      expect(control.__ran, 'the control must run, or the harness sees nothing').toBe(1);

      const filtered = run(filterMarkup(doc, type, OPTS), type);
      raise(filtered);
      expect(filtered.__ran).toBeUndefined();
    });
  }
});
