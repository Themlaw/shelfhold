import type { Page, Route } from '@playwright/test';
import { expect, test } from '../workshop/fixtures';

// M5 L12: the EPUB reader in a real browser (Chromium, Firefox, WebKit: `playwright.reader.config.ts`).
// The API of the reader and the content route are proved against the real server by the
// integration tests; here they answer with synthetic books, and what is played is what only a
// browser does: the policy of `/lire`, the `blob:` documents that inherit it, foliate's iframe,
// and the refusal of every script of a booby-trapped book. The page itself, `/lire/...`, is the
// one the real server serves, with its real header.

const ME = { id: '01HZZZZZZZZZZZZZZZZZZZZZZA', username: 'lecteur', role: 'member', nav: [] };
const NS = 'http://www.w3.org/1999/xhtml';
const T = '**/content/TICKET/';

const xhtml = (body: string, head = '') =>
  `<?xml version="1.0" encoding="utf-8"?><html xmlns="${NS}" xmlns:epub="http://www.idpf.org/2007/ops"><head><title>c</title>${head}</head><body>${body}</body></html>`;

const opf = (items: [string, string][], opts: { fixed?: boolean } = {}) =>
  `<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="i"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="i">urn:x</dc:identifier><dc:title>Un livre synthétique</dc:title><dc:language>fr</dc:language>${opts.fixed ? '<meta property="rendition:layout">pre-paginated</meta>' : ''}</metadata><manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>${items.map(([href, type], i) => `<item id="i${i}" href="${href}" media-type="${type}"/>`).join('')}</manifest><spine>${items.filter(([, t]) => t === 'application/xhtml+xml').map((_, i) => `<itemref idref="i${i}"/>`).join('')}</spine></package>`;

const nav = xhtml('<nav epub:type="toc"><ol><li><a href="c1.xhtml">Un</a></li></ol></nav>');

interface Book {
  files: Record<string, string>;
  fixed?: boolean;
}

const reflow = (chapters: string[]): Book => ({
  files: {
    'OEBPS/content.opf': opf(chapters.map((_, i) => [`c${i + 1}.xhtml`, 'application/xhtml+xml'])),
    'OEBPS/nav.xhtml': nav,
    ...Object.fromEntries(chapters.map((c, i) => [`OEBPS/c${i + 1}.xhtml`, c]))
  }
});

interface Bench {
  api: string[];
  content: string[];
  violations: string[];
}

async function serve(page: Page, book: Book): Promise<Bench> {
  const bench: Bench = { api: [], content: [], violations: [] };
  const names = Object.keys(book.files).filter((n) => n.endsWith('.xhtml') && n !== 'OEBPS/nav.xhtml');
  await page.route('**/api/v1/me', (route) => route.fulfill({ json: ME }));
  await page.route('**/api/reader/unit/U1/publication*', (route) =>
    route.fulfill({
      json: {
        carrier_id: '01HZZZZZZZZZZZZZZZZZZZZZZC', carrier_type: 'content', expires_at: '2099-01-01T00:00:00Z', kind: 'epub',
        language: 'fr', number: '1', pages: [], reading_direction: 'ltr', scope: 'full', title: 'Un livre synthétique',
        package: '/content/TICKET/OEBPS/content.opf',
        spine: names.map((n, i) => ({ index: i + 1, href: `/content/TICKET/${n}`, media_type: 'application/xhtml+xml' }))
      }
    })
  );
  await page.route('**/api/reader/positions', (route) => route.fulfill({ json: { conflict: null, position: null } }));
  await page.route(`${T}**`, (route: Route) => {
    const name = decodeURIComponent(new URL(route.request().url()).pathname.replace('/content/TICKET/', ''));
    bench.content.push(name);
    const body = book.files[name];
    return body === undefined ? route.fulfill({ status: 404, body: '' }) : route.fulfill({ body, contentType: name.endsWith('.svg') ? 'image/svg+xml' : 'application/octet-stream' });
  });
  page.on('request', (r) => {
    const path = new URL(r.url()).pathname;
    if (path.startsWith('/api/') && !/\/(v1\/me|reader\/)/.test(path)) bench.api.push(path);
  });
  await page.addInitScript(() => {
    document.addEventListener('securitypolicyviolation', (e) => ((window as unknown as { __v: string[] }).__v ??= []).push(e.violatedDirective));
  });
  return bench;
}

const frames = (page: Page) => page.frames().filter((f) => f.url().startsWith('blob:'));
const textOf = async (page: Page) => (await Promise.all(frames(page).map((f) => f.locator('body').innerText().catch(() => '')))).join('\n');

test.describe('the EPUB reader', () => {
  // req: EF-LEC-003
  test('test_foliate_epub_reflow_and_fixed: both layouts are drawn, in a blob: document', async ({ page }) => {
    await serve(page, reflow([xhtml('<h1>Chapitre un</h1><p>Texte du premier chapitre.</p>'), xhtml('<h1>Chapitre deux</h1><p>Suite.</p>')]));
    await page.goto('/lire/unit/U1');
    await expect.poll(() => textOf(page)).toContain('Chapitre un');
    expect(frames(page).length).toBeGreaterThan(0);
    await page.keyboard.press('Escape');
    await page.getByRole('button', { name: 'Page suivante' }).click();
    await expect.poll(() => page.locator('[role=status]').innerText()).not.toBe('');

    const fixedPage = (n: number) => xhtml(`<p>Page fixe ${n}</p>`, '<meta name="viewport" content="width=400, height=600"/>');
    await serve(page, {
      fixed: true,
      files: {
        'OEBPS/content.opf': opf([['c1.xhtml', 'application/xhtml+xml'], ['c2.xhtml', 'application/xhtml+xml']], { fixed: true }),
        'OEBPS/nav.xhtml': nav,
        'OEBPS/c1.xhtml': fixedPage(1),
        'OEBPS/c2.xhtml': fixedPage(2)
      }
    });
    await page.goto('/lire/unit/U1');
    await expect.poll(() => textOf(page)).toContain('Page fixe 1');
  });

  // req: ENF-SEC-004
  test('test_blob_document_inherits_reader_csp: a blob: at the top, a blob: inside it, a srcdoc', async ({ page }) => {
    await serve(page, reflow([xhtml('<p>Texte</p>')]));
    await page.goto('/lire/unit/U1');
    await expect.poll(() => textOf(page)).toContain('Texte');
    const result = await page.evaluate(async () => {
      const gif = 'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7';
      const wait = (f: HTMLIFrameElement) => new Promise<void>((r) => { f.onload = () => r(); setTimeout(r, 1500); });
      const probe = (doc: Document) => {
        const img = doc.createElement('img');
        img.src = gif;
        doc.body.append(img);
        const own = doc.createElement('img');
        own.src = URL.createObjectURL(new Blob([Uint8Array.from(atob(gif.split(',')[1]!), (c) => c.charCodeAt(0))], { type: 'image/gif' }));
        doc.body.append(own);
        return new Promise<{ dataImage: number; blobImage: number }>((r) => setTimeout(() => r({ dataImage: img.naturalWidth, blobImage: own.naturalWidth }), 600));
      };
      const html = (inner: string) => `<!doctype html><body>${inner}<script>window.__ran=1</script></body>`;
      const mount = async (src: { blob?: string; srcdoc?: string }, parent: Document) => {
        const f = parent.createElement('iframe');
        if (src.blob !== undefined) f.src = URL.createObjectURL(new Blob([src.blob], { type: 'text/html' }));
        if (src.srcdoc !== undefined) f.srcdoc = src.srcdoc;
        parent.body.append(f);
        await wait(f);
        return f;
      };
      const out: Record<string, unknown> = {};
      const top = await mount({ blob: html('') }, document);
      out.top = { ran: (top.contentWindow as unknown as { __ran?: number }).__ran ?? null, ...(await probe(top.contentDocument!)) };
      const outer = await mount({ blob: html(`<p>outer</p>`) }, document);
      const nested = await mount({ blob: html('') }, outer.contentDocument!);
      out.nested = { ran: (nested.contentWindow as unknown as { __ran?: number }).__ran ?? null, ...(await probe(nested.contentDocument!)) };
      const doc = await mount({ srcdoc: html('') }, document);
      out.srcdoc = { ran: (doc.contentWindow as unknown as { __ran?: number }).__ran ?? null, ...(await probe(doc.contentDocument!)) };
      return out as Record<'top' | 'nested' | 'srcdoc', { ran: number | null; dataImage: number; blobImage: number }>;
    });
    for (const level of ['top', 'nested', 'srcdoc'] as const) {
      expect(result[level].ran, `${level}: no script runs`).toBeNull();
      expect(result[level].dataImage, `${level}: img-src refuses data:`).toBe(0);
      expect(result[level].blobImage, `${level}: img-src admits blob:`).toBeGreaterThan(0);
    }
  });

  // req: ENF-SEC-004
  test('test_epub_script_never_runs: a book full of traps runs nothing and asks nothing of the instance', async ({ page }) => {
    const trap = xhtml(
      `<h1>Piégé</h1><script>window.__owned=1;fetch('/api/v1/keys')</script><img src="x.png" onerror="window.__owned=1"/><a id="j" href="javascript:window.__owned=1">j</a>` +
        `<svg xmlns="http://www.w3.org/2000/svg"><script>window.__owned=1</script><set attributeName="onload" to="window.__owned=1"/></svg>` +
        `<iframe srcdoc="&lt;script&gt;parent.__owned=1&lt;/script&gt;"></iframe><form action="/api/v1/keys"><button id="b">go</button></form>` +
        `<object data="/content/TICKET/OEBPS/x.js"></object><p style="background:url(/api/v1/keys)">fin</p>`,
      '<script src="/content/TICKET/OEBPS/x.js"></script><script type="module" src="/_app/immutable/entry/start.js"></script><meta http-equiv="refresh" content="0;url=/api/v1/keys"/><link rel="prefetch" href="/api/v1/keys"/><style>@import url(/api/v1/keys); p{color:red}</style>'
    );
    const book = reflow([trap]);
    book.files['OEBPS/x.js'] = 'window.__owned=1';
    const bench = await serve(page, book);
    await page.goto('/lire/unit/U1');
    await expect.poll(() => textOf(page)).toContain('Piégé');
    for (const f of frames(page)) {
      await f.locator('#j').click({ timeout: 500, trial: false }).catch(() => undefined);
      await f.locator('#b').click({ timeout: 500 }).catch(() => undefined);
    }
    await page.waitForTimeout(500);
    for (const f of [page.mainFrame(), ...frames(page)]) expect(await f.evaluate(() => (window as unknown as { __owned?: number }).__owned ?? null)).toBeNull();
    expect(bench.api).toEqual([]);
    expect(bench.content).not.toContain('OEBPS/x.js');
    expect(page.url()).toContain('/lire/unit/U1');
  });

  // req: ENF-SEC-004
  test('a script that escaped the filter and is moved by a DOM API: the filter is the barrier', async ({ page }) => {
    await serve(page, reflow([xhtml('<p>Texte</p><script>window.__owned=1</script>')]));
    await page.goto('/lire/unit/U1');
    await expect.poll(() => textOf(page)).toContain('Texte');
    // with the filter, the document of the book holds nothing to move
    const inBook = await Promise.all(frames(page).map((f) => f.evaluate(() => document.querySelectorAll('script').length)));
    expect(inBook.every((n) => n === 0)).toBe(true);
    // the limit, written down: a script that did reach a book document and that trusted code moves
    // runs under `'strict-dynamic'` (the document is the page's, `window.__owned` is set)
    const limit = await page.evaluate(async () => {
      const f = document.createElement('iframe');
      f.src = URL.createObjectURL(new Blob(['<!doctype html><body><script id="s">window.__owned=1</script></body>'], { type: 'text/html' }));
      document.body.append(f);
      await new Promise((r) => { f.onload = () => r(null); });
      const moved = document.importNode(f.contentDocument!.getElementById('s')!, true);
      document.body.append(moved);
      return (window as unknown as { __owned?: number }).__owned ?? null;
    });
    expect(limit).toBe(1);
  });

  // req: ENF-SEC-004
  test('test_external_link_never_navigates_iframe: the link is cancelled, asked, and opened apart', async ({ page, context }) => {
    await serve(page, reflow([xhtml('<p><a id="e" href="https://example.org/dehors">dehors</a> <a id="v" xmlns:x="http://www.w3.org/1999/xlink" href="#note">note</a></p><svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"><a xlink:href="https://example.org/svg"><text y="10">svg</text></a></svg>')]));
    await page.goto('/lire/unit/U1');
    await expect.poll(() => textOf(page)).toContain('dehors');
    const before = frames(page).map((f) => f.url());
    page.once('dialog', (d) => d.dismiss());
    await frames(page)[0]!.locator('#e').click();
    await page.waitForTimeout(300);
    expect(frames(page).map((f) => f.url())).toEqual(before);
    expect(context.pages()).toHaveLength(1);
    expect(page.url()).toContain('/lire/unit/U1');
  });

  // req: ENF-SEC-004
  test('test_reader_refuses_to_mount_under_spa_csp: a client navigation from the SPA is a full load', async ({ page }) => {
    await serve(page, reflow([xhtml('<p>Texte</p>')]));
    await page.goto('/lire/unit/U1');
    const csp = await page.evaluate(() => performance.getEntriesByType('navigation').length);
    expect(csp).toBe(1);
    const header = await (await page.request.get('/lire/unit/U1')).headerValue('content-security-policy');
    expect(header).toContain("'strict-dynamic'");
    expect(header).not.toContain("script-src 'self'");
    const spa = await (await page.request.get('/bibliotheque')).headerValue('content-security-policy');
    expect(spa).not.toContain('strict-dynamic');
  });
});
