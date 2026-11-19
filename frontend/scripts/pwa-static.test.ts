// What the PWA adds to the build, read from its sources: a manifest of its own origin, a worker with
// nothing dynamic, a policy of the SPA that names the worker and the manifest, and a reader that
// has none of it (ADR-0055: `worker-src 'none'`, `manifest-src 'none'`).
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { bootScript, inlineScripts, kitCsp, readerCsp } from './csp.ts';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const read = (path: string) => readFileSync(join(root, path), 'utf8');
const walk = (dir: string): string[] =>
  readdirSync(dir).flatMap((n) => (statSync(join(dir, n)).isDirectory() ? walk(join(dir, n)) : [join(dir, n)]));

describe('the manifest', () => {
  const manifest = JSON.parse(read('static/manifest.webmanifest')) as Record<string, unknown>;

  // req: EF-LEC-012
  it('names the app, opens at the root in its own window, and installs from icons of its own origin', () => {
    expect(manifest.name).toBe('Shelfhold');
    expect(manifest.start_url).toBe('/');
    expect(manifest.scope).toBe('/');
    expect(manifest.display).toBe('standalone');
    const icons = manifest.icons as { src: string; sizes: string; type: string; purpose?: string }[];
    expect(icons.some((i) => i.sizes === '192x192' && i.type === 'image/png')).toBe(true);
    expect(icons.some((i) => i.sizes === '512x512' && i.type === 'image/png')).toBe(true);
    for (const icon of icons) {
      expect(icon.src.startsWith('/')).toBe(true);
      expect(icon.src.startsWith('//')).toBe(false);
      expect(statSync(join(root, 'static', icon.src)).isFile()).toBe(true);
    }
  });

  // req: EF-LEC-012, ENF-PRIV-001
  it('holds no address of another origin and no entry that would reach the reader or the API', () => {
    const raw = read('static/manifest.webmanifest');
    expect(raw).not.toMatch(/https?:|\/\/[a-z]/i);
    expect(raw).not.toMatch(/\/lire|\/content|\/api/);
    expect(manifest).not.toHaveProperty('share_target');
    expect(manifest).not.toHaveProperty('protocol_handlers');
    expect(manifest).not.toHaveProperty('file_handlers');
    expect(manifest).not.toHaveProperty('shortcuts');
  });

  // req: EF-LEC-012, ENF-SEC-004
  it('is linked from the page of the SPA, which keeps its one hashed inline script', () => {
    const html = read('src/app.html');
    expect(html).toMatch(/<link rel="manifest" href="\/manifest\.webmanifest"\s*\/?>/);
    expect(inlineScripts(html)).toHaveLength(1);
    expect(() => bootScript(html)).not.toThrow();
  });
});

describe('the policy of the SPA', () => {
  const csp = kitCsp(read('src/app.html'));

  // req: EF-LEC-012, ENF-SEC-004
  it('service worker passes the hash CSP: the worker and the manifest are of the origin, nothing inline or dynamic is added', () => {
    expect(csp['worker-src']).toEqual(["'self'"]);
    expect(csp['manifest-src']).toEqual(["'self'"]);
    for (const [name, sources] of Object.entries(csp)) {
      if (name === 'style-src-attr') continue; // the route announcer of SvelteKit, by hash
      expect(sources, name).not.toContain("'unsafe-eval'");
      expect(sources, name).not.toContain("'unsafe-inline'");
      expect(sources, name).not.toContain('*');
    }
    expect(csp['script-src']).toEqual(["'self'", expect.stringMatching(/^'sha256-/)]);
  });
});

describe('the worker', () => {
  const sources = ['src/service-worker.ts', ...walk(join(root, 'src/lib/pwa')).map((f) => f.slice(root.length + 1)).filter((f) => !f.endsWith('.test.ts'))];

  // req: EF-LEC-012, ENF-SEC-004, ENF-PRIV-001
  it('has no eval, no Function, no importScripts, no remote address, no dynamic import', () => {
    for (const file of sources) {
      const code = read(file);
      expect(code, file).not.toMatch(/\beval\s*\(|new\s+Function|importScripts|\bimport\s*\(/);
      expect(code, file).not.toMatch(/https?:\/\//);
    }
  });

  // req: EF-LEC-012
  it('is the one place the worker is registered, and the reader never touches it', () => {
    expect(read('src/routes/+layout.svelte')).toContain('registerServiceWorker');
    for (const file of [...walk(join(root, 'src/reader')), join(root, 'reader.html')]) {
      expect(readFileSync(file, 'utf8'), file).not.toMatch(/serviceWorker|manifest/);
    }
  });

  // req: EF-LEC-012, ENF-SEC-004
  it('test_offline_reader_entry_keeps_reader_csp: the reader policy still forbids a worker and a manifest', () => {
    const page = '<!doctype html><html><head><script>boot()</script></head><body><div id="reader"></div></body></html>';
    const policy = readerCsp(page);
    expect(policy['worker-src']).toEqual(["'none'"]);
    expect(policy['manifest-src']).toEqual(["'none'"]);
  });
});
