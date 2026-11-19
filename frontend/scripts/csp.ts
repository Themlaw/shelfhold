// The CSP of the SPA (ENF-SEC-004): by hash, never `unsafe-inline`, never a random nonce (the SPA
// is static, so the hash is fixed when it is built). The one inline script is the boot script of
// `src/app.html`, which sets the theme attributes before the first render; its hash is computed
// here from the very bytes of the page, so the CSP cannot drift from the script that is served.
import { createHash } from 'node:crypto';
import { parse } from 'parse5';

type Node = { nodeName: string; value?: string; childNodes?: Node[]; attrs?: { name: string; value: string }[] };

/** The body of every inline script of `html`, read by a real HTML parser: a regex on tags is
 * bypassed by `</script >`, case, attributes or a comment holding `<script>`. */
export function inlineScripts(html: string): string[] {
  const found: string[] = [];
  const walk = (node: Node) => {
    if (node.nodeName === 'script' && !node.attrs?.some((a) => a.name === 'src')) {
      found.push((node.childNodes ?? []).map((child) => child.value ?? '').join(''));
    }
    for (const child of node.childNodes ?? []) walk(child);
  };
  walk(parse(html) as unknown as Node);
  return found;
}

/** The text of the single inline script of `app.html`. */
export function bootScript(html: string): string {
  const scripts = inlineScripts(html);
  const [only] = scripts;
  if (scripts.length !== 1 || only === undefined) {
    throw new Error(`app.html must hold exactly one inline script, not ${scripts.length}`);
  }
  return only;
}

/** A CSP source expression for `text`: `'sha256-<base64>'`. */
export function scriptHash(text: string): string {
  return `'sha256-${createHash('sha256').update(text).digest('base64')}'`;
}

// SvelteKit's route announcer carries a style attribute; allow only that hash.
const ANNOUNCER_STYLE = "'sha256-S8qMpvofolR8Mpjy4kQvEm7m1q8clzU4dfDH0AmvZjo='";

/** The `csp.directives` of SvelteKit (hash mode adds the hashes of its own inline script). */
export function kitCsp(html: string): Record<string, string[]> {
  return {
    'default-src': ["'self'"],
    'script-src': ["'self'", scriptHash(bootScript(html))],
    'style-src': ["'self'"],
    // the reader (`/lire`) is its own entry with its own policy (L12): nothing of the SPA draws a
    // `blob:` image, no `data:`, no host.
    'img-src': ["'self'"],
    // the PWA (L16a): its worker and its manifest are files of the origin (the reader has neither)
    'worker-src': ["'self'"],
    'manifest-src': ["'self'"],
    'base-uri': ["'none'"],
    'form-action': ["'self'"],
    'object-src': ["'none'"],
    'style-src-attr': ["'unsafe-hashes'", ANNOUNCER_STYLE]
  };
}

// ---------------------------------------------------------------------------------------------
// The reader (`/lire`, ADR-0055): its own HTML entry, outside the router, with the policy of the
// table of the ADR. The server writes the header itself from the page (`reader_csp`), and this is
// its twin for the build and the tests: both refuse a page that is not the audited one.

const PRELOADS = /^(modulepreload|preload|prefetch|preconnect|dns-prefetch|prerender)$/i;

/** What the page of the reader must not hold: a `<meta>` policy, a second script, a script file, a
 * preload (refused under `'strict-dynamic'`), a remote link, a handler. Throws on the first one. */
export function auditReaderPage(html: string): void {
  const refuse = (why: string): never => {
    throw new Error(`the reader page is not the audited one: ${why}`);
  };
  const walk = (node: Node) => {
    if (node.nodeName === '#text' || node.nodeName === '#comment') return;
    const attr = (name: string) => node.attrs?.find((a) => a.name === name)?.value;
    if (node.nodeName === 'meta' && attr('http-equiv') !== undefined) refuse('it carries a <meta http-equiv>');
    if (node.nodeName === 'script' && attr('src') !== undefined) refuse('it loads a script file');
    if (node.nodeName === 'link') {
      const rel = (attr('rel') ?? '').split(/\s+/);
      if (rel.some((r) => PRELOADS.test(r))) refuse('it holds a preload');
      const href = attr('href') ?? '';
      if (!rel.includes('stylesheet') || !href.startsWith('/reader-entry/')) refuse(`a link that is not its own style sheet: ${href}`);
    }
    for (const a of node.attrs ?? []) {
      if (/^on/i.test(a.name)) refuse(`a handler: ${a.name}`);
      if (/^(src|href|action|data|poster)$/i.test(a.name) && /^(https?:)?\/\//i.test(a.value)) refuse(`a remote address: ${a.name}`);
    }
    for (const child of node.childNodes ?? []) walk(child);
  };
  walk(parse(html) as unknown as Node);
  const scripts = inlineScripts(html);
  if (scripts.length !== 1) refuse(`it has ${scripts.length} inline scripts, not exactly one`);
}

/** The policy of `/lire`: ADR-0055, "CSP du lecteur, directive par directive". */
export function readerCsp(html: string): Record<string, string[]> {
  auditReaderPage(html);
  const [boot] = inlineScripts(html);
  return {
    'default-src': ["'none'"],
    'script-src': [scriptHash(boot ?? ''), "'strict-dynamic'"],
    'style-src': ["'self'", 'blob:', "'unsafe-inline'"],
    'style-src-elem': ["'self'", 'blob:', "'unsafe-inline'"],
    'style-src-attr': ["'unsafe-inline'"],
    'img-src': ["'self'", 'blob:'],
    'font-src': ["'self'", 'blob:'],
    'media-src': ["'none'"],
    'frame-src': ['blob:'],
    'worker-src': ["'none'"],
    'manifest-src': ["'none'"],
    'connect-src': ["'self'"],
    'object-src': ["'none'"],
    'base-uri': ["'none'"],
    'form-action': ["'none'"]
  };
}
