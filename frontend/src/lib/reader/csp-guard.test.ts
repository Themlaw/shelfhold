// The reader refuses to mount under a policy that is not its own (ADR-0055, "CSP du lecteur"):
// a route of the SPA carries the SPA's `<meta>`, and the two policies would apply together.
import { describe, expect, it } from 'vitest';
import { browserProbes, checkReaderCsp, type GuardProbes } from './csp-guard.ts';

const probes = (over: Partial<GuardProbes> = {}): GuardProbes => ({
  inlineStyleAllowed: async () => true,
  forbiddenImageBlocked: async () => true,
  ...over
});

const withHead = (html: string): Document => new DOMParser().parseFromString(`<!doctype html><head>${html}</head><body></body>`, 'text/html');

describe('the guard of the reader', () => {
  // req: ENF-SEC-004
  it('mounts under the policy of the reader: a bare page, inline style allowed, a forbidden image blocked', async () => {
    expect(await checkReaderCsp(withHead(''), probes())).toBe('ok');
  });

  // req: ENF-SEC-004
  it('test_reader_refuses_to_mount_under_spa_csp: a <meta> policy of the SPA in the document', async () => {
    const meta = `<meta http-equiv="Content-Security-Policy" content="default-src 'self'; style-src 'self'">`;
    expect(await checkReaderCsp(withHead(meta), probes())).toBe('refused');
    expect(await checkReaderCsp(withHead(meta.toUpperCase()), probes())).toBe('refused');
  });

  // req: ENF-SEC-004
  it('refuses when the inline style of the reader is blocked: the policy in force is the SPA\'s', async () => {
    expect(await checkReaderCsp(withHead(''), probes({ inlineStyleAllowed: async () => false }))).toBe('refused');
  });

  // req: ENF-SEC-004
  it('refuses when a resource the reader forbids is not blocked: no policy, or a looser one', async () => {
    expect(await checkReaderCsp(withHead(''), probes({ forbiddenImageBlocked: async () => false }))).toBe('refused');
  });

  // req: ENF-SEC-004
  it('refuses a page of the SPA: its script of the router, its markers', async () => {
    expect(await checkReaderCsp(withHead('<script type="module" src="/_app/immutable/entry/start.js"></script>'), probes())).toBe('refused');
    const doc = withHead('');
    doc.body.setAttribute('data-sveltekit-preload-data', 'hover');
    expect(await checkReaderCsp(doc, probes())).toBe('refused');
  });

  // req: ENF-SEC-004
  it('refuses when a probe cannot answer', async () => {
    const broken = probes({ inlineStyleAllowed: async () => Promise.reject(new Error('no')) });
    expect(await checkReaderCsp(withHead(''), broken)).toBe('refused');
  });
});

describe('the probes of the browser', () => {
  const violation = (doc: Document, effectiveDirective: string, after = 5) =>
    setTimeout(() => {
      const event = new Event('securitypolicyviolation') as Event & { effectiveDirective: string };
      event.effectiveDirective = effectiveDirective;
      doc.dispatchEvent(event);
    }, after);

  // req: ENF-SEC-004
  it('sees an inline style refused by the policy in force', async () => {
    const doc = document.implementation.createHTMLDocument('p');
    violation(doc, 'style-src-elem');
    expect(await browserProbes(doc).inlineStyleAllowed()).toBe(false);
  });

  // req: ENF-SEC-004
  it('sees an inline style applied when nothing refuses it', async () => {
    expect(await browserProbes(document).inlineStyleAllowed()).toBe(true);
  });

  const served = (policy: string | null) => {
    const view = document.implementation.createHTMLDocument('p');
    const headers = new Headers();
    if (policy !== null) headers.set('content-security-policy', policy);
    Object.defineProperty(view, 'defaultView', {
      value: {
        location: { href: 'https://shelf.example/lire' },
        fetch: async () => new Response('x', { headers })
      }
    });
    return view;
  };

  // req: ENF-SEC-004
  it('accepts the policy of the reader and refuses one that lets a data: image through', async () => {
    const ok = "default-src 'none'; img-src 'self' blob:";
    expect(await browserProbes(served(ok)).forbiddenImageBlocked()).toBe(true);
    expect(await browserProbes(served("img-src 'self' data:")).forbiddenImageBlocked()).toBe(false);
    expect(await browserProbes(served('img-src *')).forbiddenImageBlocked()).toBe(false);
    expect(await browserProbes(served("default-src 'none'")).forbiddenImageBlocked()).toBe(false);
    expect(await browserProbes(served(null)).forbiddenImageBlocked()).toBe(false);
  });

  // req: ENF-SEC-004
  it('provokes no refusal of its own: nothing for the console or the listeners of the page', async () => {
    const seen: string[] = [];
    const listener = (event: Event) => seen.push(event.type);
    document.addEventListener('securitypolicyviolation', listener);
    try {
      await browserProbes(served("img-src 'self' blob:")).forbiddenImageBlocked();
      expect(seen).toEqual([]);
    } finally {
      document.removeEventListener('securitypolicyviolation', listener);
    }
  });
});
