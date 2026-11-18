// The filter alone, with no policy at all (an EPUB `publisher` whose CSP was taken away): what it
// returns must not run a script in a document that has no CSP, whatever the vector. The control is
// the same document unfiltered, which does run: the harness would see it.
import { JSDOM, VirtualConsole } from 'jsdom';
import { describe, expect, it } from 'vitest';
import { filterMarkup } from './filter.ts';

const OPTS = { origin: 'https://shelfhold.test' };
const NS = 'xmlns="http://www.w3.org/1999/xhtml"';
const run = (xhtml: string) => {
  const dom = new JSDOM(xhtml, { contentType: 'application/xhtml+xml', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: new VirtualConsole() });
  return dom.window as unknown as Window & { __ran?: number; document: Document };
};
const body = (inner: string) => `<html ${NS}><head><title>t</title></head><body>${inner}</body></html>`;

const vectors: Record<string, string> = {
  'inline script': body('<script>window.__ran=1</script>'),
  'on* on an image': body('<img src="blob:x" onerror="window.__ran=1" onload="window.__ran=1"/>'),
  'on* on body': `<html ${NS}><head><title>t</title></head><body onload="window.__ran=1"><p>x</p></body></html>`,
  'svg script': body('<svg xmlns="http://www.w3.org/2000/svg"><script>window.__ran=1</script></svg>'),
  'svg on*': body('<svg xmlns="http://www.w3.org/2000/svg" onload="window.__ran=1"><rect onclick="window.__ran=1" width="1" height="1"/></svg>'),
  'javascript: link': body('<a id="l" href="javascript:window.__ran=1">x</a>'),
  'iframe srcdoc': body('<iframe srcdoc="&lt;script&gt;parent.__ran=1&lt;/script&gt;"></iframe>'),
  'object data': body('<object data="data:text/html,x"></object>'),
  'form with javascript action': body('<form id="f" action="javascript:window.__ran=1"><button id="b">go</button></form>')
};

describe('the filter alone, with no policy', () => {
  // req: ENF-SEC-004
  for (const [name, doc] of Object.entries(vectors)) {
    it(`test_epub_script_never_runs (filter alone): ${name}`, () => {
      const control = run(doc);
      control.document.querySelector<HTMLElement>('#l, #b')?.click();
      expect(control.__ran, 'the control must run, or the harness sees nothing').toBe(1);

      const window = run(filterMarkup(doc, 'application/xhtml+xml', OPTS));
      window.document.querySelector<HTMLElement>('#l, #b')?.click();
      expect(window.__ran).toBeUndefined();
    });
  }
});
