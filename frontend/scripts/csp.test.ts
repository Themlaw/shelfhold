import { createHash } from 'node:crypto';
import { existsSync, readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { parse } from 'parse5';
import { describe, expect, it } from 'vitest';
import { bootScript, inlineScripts, kitCsp, scriptHash } from './csp.ts';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const appHtml = readFileSync(join(root, 'src', 'app.html'), 'utf8');
const sha = (text: string) => `sha256-${createHash('sha256').update(text).digest('base64')}`;

function cspOfMeta(html: string): string | null {
  const find = (node: { nodeName: string; attrs?: { name: string; value: string }[]; childNodes?: unknown[] }): string | null => {
    if (node.nodeName === 'meta') {
      const attr = (n: string) => node.attrs?.find((a) => a.name === n)?.value;
      if (attr('http-equiv')?.toLowerCase() === 'content-security-policy') return attr('content') ?? '';
    }
    for (const child of (node.childNodes ?? []) as (typeof node)[]) {
      const hit = find(child);
      if (hit !== null) return hit;
    }
    return null;
  };
  return find(parse(html) as never);
}

describe('SPA CSP', () => {
  // req: ENF-SEC-004
  it('hashes the boot script of app.html, byte for byte', () => {
    const script = bootScript(appHtml);
    expect(script.length).toBeGreaterThan(20);
    expect(scriptHash(script)).toBe(`'${sha(script)}'`);
    expect(kitCsp(appHtml)['script-src']).toContain(`'${sha(script)}'`);
  });

  // req: ENF-SEC-004
  it('a changed boot script no longer matches the hash that was computed', () => {
    const before = kitCsp(appHtml)['script-src'];
    const tampered = appHtml.replace('dataset', 'dataSet');
    expect(tampered).not.toBe(appHtml);
    expect(kitCsp(tampered)['script-src']).not.toEqual(before);
  });

  // req: ENF-SEC-004
  it('has no unsafe-inline, unsafe-eval or nonce for scripts, and none for styles', () => {
    const csp = kitCsp(appHtml);
    for (const name of ['default-src', 'script-src', 'style-src']) {
      expect(csp[name], name).not.toContain("'unsafe-inline'");
      expect(csp[name], name).not.toContain("'unsafe-eval'");
    }
    expect(JSON.stringify(csp)).not.toContain('nonce');
    expect(csp['style-src']).toEqual(["'self'"]);
    expect(csp['object-src']).toEqual(["'none'"]);
    expect(csp['base-uri']).toEqual(["'none'"]);
    // the one inline style attribute (the route announcer) is a hash, with `unsafe-hashes`
    expect(csp['style-src-attr']).toContain("'unsafe-hashes'");
    expect(csp['style-src-attr']!.filter((v) => v.startsWith("'sha256-")).length).toBe(1);
  });

  // req: ENF-SEC-004
  it('app.html has exactly one inline script, and no inline style or handler', () => {
    expect(appHtml.match(/<script\b/g)?.length).toBe(1);
    expect(appHtml).not.toMatch(/<style\b/);
    expect(appHtml).not.toMatch(/\son[a-z]+\s*=/i);
    expect(appHtml).not.toMatch(/\sstyle\s*=/i);
    expect(appHtml).not.toMatch(/https?:\/\//); // nothing third party (ENF-PRIV-001)
  });

  // req: ENF-SEC-004
  it.each([
    ['</SCRIPT>', '<script>a()</SCRIPT>'],
    ['</script >', '<script>a()</script >'],
    ['a tab before >', '<script>a()</script\t>'],
    ['mixed case open tag', '<ScRiPt>a()</sCrIpT>']
  ])('reads an inline script closed by %s', (_name, markup) => {
    expect(inlineScripts(`<!doctype html><head>${markup}</head>`)).toEqual(['a()']);
  });

  // req: ENF-SEC-004
  it('does not take a script inside an HTML comment for one, nor miss the one after it', () => {
    const html = '<!doctype html><head><!-- <script>evil()</script> --><script>real()</script></head>';
    expect(inlineScripts(html)).toEqual(['real()']);
    expect(() => bootScript('<!doctype html><head><!-- <script>x()</script> --></head>')).toThrow();
  });

  // req: ENF-SEC-004
  it('leaves out a script with a src, keeps one with attributes', () => {
    const html = '<head><script src="/a.js"></script><script type="module">m()</script></head>';
    expect(inlineScripts(html)).toEqual(['m()']);
  });

  // req: ENF-SEC-004
  it('the vite config takes its CSP from kitCsp, not from a literal', () => {
    const config = readFileSync(join(root, 'vite.config.ts'), 'utf8');
    expect(config).toContain('kitCsp(');
    expect(config).not.toContain('unsafe-inline');
  });

  // req: ENF-SEC-004
  it.skipIf(!existsSync(join(root, 'build', 'index.html')))(
    'the built index.html carries the hash of the script it serves',
    () => {
      const built = readFileSync(join(root, 'build', 'index.html'), 'utf8');
      const csp = cspOfMeta(built);
      expect(csp, 'a CSP meta in the build').not.toBeNull();
      expect(csp).not.toContain('unsafe-inline');
      expect(inlineScripts(built).length).toBeGreaterThan(0);
      for (const body of inlineScripts(built)) {
        expect(csp, `inline script ${body.slice(0, 30)}`).toContain(`'${sha(body)}'`);
      }
    }
  );
});
