// The build of the reader (ADR-0055, docs/dependencies.md "consigne pour L12"): its own HTML entry
// outside the router, with no `<meta>` policy and no preload, and a bundle that holds the EPUB
// engine alone: no PDF, MOBI, FB2 or comic-book chunk, no speech, no footnote mover.
import { execFileSync } from 'node:child_process';
import { mkdtempSync, readFileSync, readdirSync, rmSync, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { inlineScripts, readerCsp, scriptHash } from './csp.ts';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
let out = '';

const walk = (dir: string): string[] =>
  readdirSync(dir).flatMap((n) => (statSync(join(dir, n)).isDirectory() ? walk(join(dir, n)) : [join(dir, n)]));

beforeAll(() => {
  out = mkdtempSync(join(tmpdir(), 'reader-build-'));
  execFileSync(process.execPath, [join(root, 'node_modules/vite/bin/vite.js'), 'build', '--config', 'vite.reader.config.ts', '--outDir', out, '--emptyOutDir', '--logLevel', 'error'], {
    cwd: root,
    stdio: 'pipe'
  });
}, 120_000);
afterAll(() => rmSync(out, { recursive: true, force: true }));

describe('the build of the reader', () => {
  const html = () => readFileSync(join(out, 'reader.html'), 'utf8');

  // req: ENF-SEC-004
  it('test_reader_entry_has_no_spa_meta_csp: a page of its own, with no <meta> policy and no preload', () => {
    const page = html();
    expect(page).not.toMatch(/http-equiv/i);
    expect(page).not.toMatch(/modulepreload|rel="?(prefetch|preload|preconnect|dns-prefetch)/i);
    expect(page).not.toMatch(/sveltekit|_app\//i);
    expect(page).not.toMatch(/<script[^>]*\ssrc=/i);
    expect(page).not.toMatch(/\son[a-z]+=/i);
  });

  // req: ENF-SEC-004
  it('links its own style sheet, which the build produced', () => {
    const link = /<link rel="stylesheet" href="(\/reader-entry\/assets\/[^"]+\.css)">/.exec(html());
    expect(link).not.toBeNull();
    expect(walk(out).some((f) => f.endsWith(link![1]!.replace('/reader-entry', '')))).toBe(true);
  });

  // req: ENF-SEC-004
  it('has one inline script, hashed by the policy, that adds the entry of its own base as a script element', () => {
    const scripts = inlineScripts(html());
    expect(scripts).toHaveLength(1);
    // not a bare import(): under 'strict-dynamic' the browser refuses that one from a parser-inserted script
    expect(scripts[0]).toMatch(/^document\.head\.append\(Object\.assign\(document\.createElement\("script"\),\{type:"module",src:"\/reader-entry\/assets\/[^"]+\.js"\}\)\);$/);
    expect(readerCsp(html())['script-src']).toEqual([scriptHash(scripts[0]!), "'strict-dynamic'"]);
  });

  // req: EF-LEC-003
  it('holds no PDF, MOBI, FB2 or comic-book chunk, no speech and no footnote mover', () => {
    const files = walk(out);
    const names = files.map((f) => f.slice(out.length)).join('\n');
    expect(names).not.toMatch(/pdf|mobi|fb2|comic|fflate|zip|tts|footnotes|quote-image|opds|dict/i);
    for (const file of files.filter((f) => f.endsWith('.js'))) {
      const text = readFileSync(file, 'utf8');
      if (/_shelfhold-foliate-not-read/.test(file)) {
        // the one module that stands in for every format that is not read: a few bytes that throw
        expect(text.length, file).toBeLessThan(600);
        expect(text).toContain('is not read by the reader of Shelfhold');
        continue;
      }
      expect(text, file).not.toMatch(/getDocument|pdfjs|gribuser|PalmDOC|BOOKMOBI|FictionBook|SpeechSynthesis|FootnoteHandler|SSML|quote-image/);
    }
  });

  // req: EF-LEC-003
  it('holds the EPUB engine: the loader, the paginator, the fixed layout, the overlayer', () => {
    const all = walk(out).filter((f) => f.endsWith('.js')).map((f) => readFileSync(f, 'utf8')).join('\n');
    expect(all).toContain('foliate-paginator');
    expect(all).toContain('foliate-fxl');
    expect(all).toContain('transformTarget');
  });
});
