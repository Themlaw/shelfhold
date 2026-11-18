// The content filter of the EPUB reader (ADR-0055, "Filtre de contenu"): a whitelist, applied to
// every EPUB whatever its trust level, before a `blob:` is made. These tests are the corpus of
// booby-trapped books, read by a parser of their own (not the filter's) to see what is left.
import { describe, expect, it } from 'vitest';
import {
  CONTENT_FILTER,
  FilterRefusal,
  filterCss,
  filterMarkup,
  installFilter,
  isLoadable,
  mediaTypeFor
} from './filter.ts';

const ORIGIN = 'https://shelfhold.test';
const LOADER = `blob:${ORIGIN}/6f1c2d7e-0000-4000-8000-000000000001`;
const OPTS = { origin: ORIGIN };
const XHTML = 'application/xhtml+xml';
const SVG = 'image/svg+xml';

const page = (body: string, head = '') =>
  `<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><head><title>t</title>${head}</head><body>${body}</body></html>`;
const svg = (inner: string) =>
  `<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 10 10">${inner}</svg>`;

const parse = (text: string, type = XHTML) => new DOMParser().parseFromString(text, type);
const run = (text: string, type = XHTML) => parse(filterMarkup(text, type, OPTS), type);

/** Everything a book must never keep, whatever the vector (independent of the filter's lists). */
function dangers(doc: Document): string[] {
  const found: string[] = [];
  const BAD = /^(script|iframe|frame|frameset|object|embed|applet|form|input|button|select|textarea|base|portal|foreignobject|animate|set|animatemotion|animatetransform|maction|annotation-xml|noscript|template|dialog)$/i;
  for (const el of Array.from(doc.querySelectorAll('*'))) {
    const name = el.localName;
    if (BAD.test(name)) found.push(`element ${name}`);
    if (name === 'link' && !(el.getAttribute('rel') === 'stylesheet' && el.getAttribute('href')?.startsWith('blob:')))
      found.push('link');
    for (const attr of Array.from(el.attributes)) {
      const v = attr.value.replace(/[\u0000-\u0020]/g, '').toLowerCase();
      if (/^on/i.test(attr.name)) found.push(`handler ${attr.name}`);
      if (/^(srcdoc|formaction|ping|http-equiv)$/i.test(attr.name)) found.push(`attr ${attr.name}`);
      if (/^(javascript|data|vbscript):/.test(v)) found.push(`scheme in ${attr.name}`);
      if (/url\(|@import|expression\(|behavior|-moz-binding/.test(v) && !/url\(["']?(blob:|#)/.test(v))
        found.push(`css in ${attr.name}`);
      if (/^(src|poster|data|action|background|cite|xlink:href|srcset)$/i.test(attr.name) && v && !v.startsWith('blob:') && !v.startsWith('#'))
        found.push(`url in ${attr.name}`);
      if (/^(href)$/i.test(attr.name) && name !== 'a' && v && !v.startsWith('blob:') && !v.startsWith('#'))
        found.push(`href on ${name}`);
      if (v.includes(ORIGIN.replace('https://', '')) || /^\/(?!\/)|^\/\//.test(v) && !v.startsWith('#'))
        if (/^(src|href|data|poster|action|background|xlink:href)$/i.test(attr.name) && name !== 'a') found.push(`instance url in ${attr.name}`);
    }
    if (name === 'style' && /@import|url\((?!["']?(blob:|#))|expression|behavior/i.test(el.textContent ?? ''))
      found.push('style element');
  }
  for (let n = doc.firstChild; n; n = n.nextSibling) if (n.nodeType === 7) found.push('processing instruction');
  return found;
}

describe('the types of the blobs', () => {
  // req: ENF-SEC-004
  it('are fixed by the name the loader read, never by the type the OPF declares', () => {
    expect(mediaTypeFor('OEBPS/c1.xhtml')).toBe(XHTML);
    expect(mediaTypeFor('a/b.XHTML')).toBe(XHTML);
    expect(mediaTypeFor('a/b.svg')).toBe(SVG);
    expect(mediaTypeFor('a/b.css')).toBe('text/css');
    expect(mediaTypeFor('a/b.png')).toBe('image/png');
    expect(mediaTypeFor('a/b.jpeg')).toBe('image/jpeg');
    expect(mediaTypeFor('a/b.woff2')).toBe('font/woff2');
    for (const name of ['x.js', 'x.mjs', 'x.html.js', 'x.wasm', 'x.json', 'x.xml', 'x.pdf', 'x.mp3', 'x', 'x.xhtml.txt', 'x.svgz'])
      expect(mediaTypeFor(name), name).toBe('application/octet-stream');
  });

  // req: ENF-SEC-004
  it('refuses to load a script, whatever its declared type, and what is not whitelisted', () => {
    expect(isLoadable('text/javascript', true)).toBe(false);
    expect(isLoadable('application/xhtml+xml', true)).toBe(false);
    for (const type of ['application/javascript', 'application/x-javascript', 'text/ecmascript', 'application/wasm', 'application/json', 'text/html', 'application/pdf', 'video/mp4', 'application/octet-stream', ''])
      expect(isLoadable(type, false), type).toBe(false);
    for (const type of [XHTML, SVG, 'text/css', 'image/png', 'image/jpeg', 'image/gif', 'image/webp', 'font/woff2', 'application/font-woff', 'application/vnd.ms-opentype', 'application/x-font-ttf'])
      expect(isLoadable(type, false), type).toBe(true);
  });
});

describe('the whitelist corpus', () => {
  const corpus: Record<string, string> = {
    'script en ligne': page('<p>a</p><script>fetch("/api/v1/auth/session")</script><p>b</p>'),
    'script src absolu': page('<script src="https://evil.example/x.js"></script>'),
    'script src relatif': page('<script src="../x.js"></script>'),
    'script src vers /content': page('<script src="/content/TICKET/x.js"></script>'),
    'script module vers la SPA': page('<script type="module" src="/_app/immutable/entry/start.js"></script>'),
    'on* en minuscules': page('<p onclick="x()" onmouseover="y()">t</p>'),
    'on* en majuscules': page('<p ONCLICK="x()" OnError="y()">t</p><img src="blob:x" onerror="z()"/>'),
    'javascript: dans un lien': page('<a href="javascript:alert(1)">x</a>'),
    'javascript: avec tabulation et casse': page('<a href="  JaVa&#9;Script:alert(1)">x</a>'),
    'javascript: dans xlink:href': page(svg('<a xlink:href="javascript:alert(1)"><text>x</text></a>')),
    'meta refresh': page('<p>x</p>', '<meta http-equiv="refresh" content="0;url=https://evil.example"/>'),
    'autre http-equiv': page('<p>x</p>', '<meta http-equiv="Content-Security-Policy" content="default-src *"/>'),
    base: page('<p>x</p>', '<base href="https://evil.example/"/>'),
    'link prefetch': page('<p>x</p>', '<link rel="prefetch" href="https://evil.example/a"/><link rel="dns-prefetch" href="//evil.example"/><link rel="preconnect" href="https://evil.example"/>'),
    'link stylesheet distant': page('<p>x</p>', '<link rel="stylesheet" href="https://evil.example/a.css"/>'),
    '@import distant en style': page('<p>x</p>', '<style>@import url("https://evil.example/a.css"); p{color:red}</style>'),
    '@import en chaine': page('<p>x</p>', "<style>@import 'https://evil.example/a.css';</style>"),
    'url() distant': page('<p style="background:url(https://evil.example/p.png)">x</p>'),
    'url() vers l’instance': page('<p style="background:url(/api/v1/auth/session)">x</p>', '<style>p{background:url(//shelfhold.test/api/x)}</style>'),
    'url() échappé': page('<p style="background:u\\72l(https://evil.example/p.png)">x</p>'),
    'url() sans guillemets, espaces': page('<style>p{background:url(  https://evil.example/p.png  )}</style>'),
    'image-set': page('<style>p{background:image-set("https://evil.example/p.png" 1x)}</style>'),
    expression: page('<p style="width:expression(alert(1))">x</p>'),
    behavior: page('<style>p{behavior:url(x.htc);-moz-binding:url(x.xml#a)}</style>'),
    'SVG avec script': page(svg('<script>alert(1)</script><rect width="1" height="1"/>')),
    'SVG set vers javascript:': page(svg('<a xlink:href="#"><set attributeName="href" to="javascript:alert(1)"/></a>')),
    'SVG animate': page(svg('<a><animate attributeName="href" values="javascript:alert(1)"/></a>')),
    'SVG foreignObject': page(svg('<foreignObject><body xmlns="http://www.w3.org/1999/xhtml"><script>alert(1)</script></body></foreignObject>')),
    'SVG use externe': page(svg('<use xlink:href="https://evil.example/x.svg#a"/><use href="/content/T/x.svg#a"/>')),
    'SVG avec on*': page(svg('<rect width="1" height="1" onload="alert(1)" onclick="alert(2)"/>')),
    'SVG fill distant': page(svg('<rect width="1" height="1" fill="url(https://evil.example/p)"/>')),
    'iframe srcdoc': page('<iframe srcdoc="&lt;script&gt;alert(1)&lt;/script&gt;"></iframe>'),
    'iframe blob imbriquée': page('<iframe src="blob:https://shelfhold.test/other"></iframe>'),
    'iframe imbriquée avec script': page('<iframe><script>alert(1)</script></iframe>'),
    'object data': page('<object data="https://evil.example/x.swf"></object><embed src="https://evil.example/x.swf"/>'),
    formulaire: page('<form action="https://evil.example/" method="post"><input name="t"/><button formaction="https://evil.example/">go</button><select><option>1</option></select><textarea>t</textarea></form>'),
    'MathML href et maction': page('<math xmlns="http://www.w3.org/1998/Math/MathML" href="javascript:alert(1)"><maction actiontype="statusline"><mi>x</mi></maction><semantics><annotation-xml encoding="text/html"><script>alert(1)</script></annotation-xml></semantics></math>'),
    'data:text/html': page('<a href="data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==">x</a><img src="data:image/svg+xml,&lt;svg onload=alert(1)&gt;"/>'),
    'srcset distant': page('<img src="blob:x" srcset="https://evil.example/a.png 1x, /api/x 2x"/>'),
    'processing instruction': `<?xml version="1.0"?><?xml-stylesheet href="https://evil.example/a.css" type="text/css"?><html xmlns="http://www.w3.org/1999/xhtml"><head><title>t</title></head><body><p>x</p></body></html>`,
    'ping et formaction': page('<a href="#" ping="https://evil.example/p">x</a><button formaction="/api/x">y</button>'),
    portal: page('<portal src="https://evil.example/"></portal>'),
    'poster et background': page('<video poster="https://evil.example/p.png"></video><table background="https://evil.example/b.png"><tr><td>x</td></tr></table>')
  };

  // req: ENF-SEC-004
  for (const [name, text] of Object.entries(corpus)) {
    it(`test_content_filter_whitelist_corpus: leaves nothing of "${name}"`, () => {
      const out = filterMarkup(text, XHTML, OPTS);
      expect(dangers(parse(out)), out).toEqual([]);
      // and nothing of the trap survives as text of an attribute either
      expect(out.toLowerCase()).not.toContain('evil.example');
      expect(out.toLowerCase()).not.toMatch(/javascript:|alert\(|onerror|onclick|srcdoc/);
    });
  }

  // req: ENF-SEC-004
  it('keeps what a book is made of: text, structure, its own images and sheets, internal and external links', () => {
    const src = page(
      `<h1 id="t" class="x" lang="fr" epub:type="chapter">Titre</h1><p style="color:red;margin:0">Un <em>texte</em> et <a href="chap2.xhtml#a">un lien</a>, <a href="#n1">une note</a>, <a href="https://example.org/page">dehors</a>.</p>` +
        `<img src="${LOADER}" alt="figure" width="10"/><table><tr><td colspan="2">c</td></tr></table><ruby>漢<rt>kan</rt></ruby>` +
        svg(`<image xlink:href="${LOADER}" width="1" height="1"/><use xlink:href="#sym"/><text x="1">a</text>`),
      `<link rel="stylesheet" type="text/css" href="${LOADER}"/><style>p{font-family:"X";background:url("${LOADER}")} @font-face{font-family:X;src:url(${LOADER})}</style>`
    );
    const doc = run(src);
    expect(doc.querySelector('h1')?.textContent).toBe('Titre');
    expect(doc.querySelector('h1')?.getAttribute('epub:type')).toBe('chapter');
    expect(doc.querySelector('p')?.getAttribute('style')).toContain('color:red');
    expect(Array.from(doc.querySelectorAll('a')).map((a) => a.getAttribute('href'))).toEqual(['chap2.xhtml#a', '#n1', 'https://example.org/page']);
    expect(doc.querySelector('img')?.getAttribute('src')).toBe(LOADER);
    expect(doc.querySelector('link')?.getAttribute('href')).toBe(LOADER);
    expect(doc.querySelector('style')?.textContent).toContain(LOADER);
    expect(doc.querySelector('td')?.getAttribute('colspan')).toBe('2');
    expect(doc.querySelector('rt')?.textContent).toBe('kan');
    expect(doc.querySelector('image')?.getAttributeNS('http://www.w3.org/1999/xlink', 'href')).toBe(LOADER);
    expect(doc.querySelector('use')?.getAttributeNS('http://www.w3.org/1999/xlink', 'href')).toBe('#sym');
    expect(doc.documentElement.namespaceURI).toBe('http://www.w3.org/1999/xhtml');
  });

  // req: ENF-SEC-004
  it('refuses a link the instance would answer (root-relative, scheme-relative, its own origin)', () => {
    const out = filterMarkup(page('<a href="/api/v1/keys">a</a><a href="//shelfhold.test/api">b</a><a href="https://shelfhold.test/api">c</a><a href="http://shelfhold.test:8080/x">d</a><img src="/content/T/p.png"/>'), XHTML, OPTS);
    expect(out).not.toMatch(/\/api|\/content|shelfhold\.test/);
  });

  // req: ENF-SEC-004
  it('refuses a blob: that this origin did not make', () => {
    const out = filterMarkup(page('<img src="blob:https://evil.example/abc"/><img src="blob:null/abc"/>'), XHTML, OPTS);
    expect(out).not.toContain('blob:');
  });

  // req: ENF-SEC-004
  it('replaces an unknown element by an inert one rather than letting it through (whitelist)', () => {
    const doc = run(page('<p>a</p><made-up-element onclick="x()">t</made-up-element><blink>b</blink>'));
    expect(doc.querySelector('made-up-element, blink')).toBeNull();
    expect(doc.querySelectorAll('body > *').length).toBe(3);
  });

  // req: ENF-SEC-004
  it('refuses invalid XHTML instead of parsing it again as HTML', () => {
    expect(() => filterMarkup('<html><body><p>unclosed<script>alert(1)</script></body>', XHTML, OPTS)).toThrow(FilterRefusal);
    expect(() => filterMarkup('<p>not even a document', XHTML, OPTS)).toThrow(FilterRefusal);
    expect(() => filterMarkup('<svg xmlns="http://www.w3.org/2000/svg"><g></svg>', SVG, OPTS)).toThrow(FilterRefusal);
  });

  // req: ENF-SEC-004
  it('refuses a document whose root is not the one its type says', () => {
    expect(() => filterMarkup(svg('<rect/>'), XHTML, OPTS)).toThrow(FilterRefusal);
    expect(() => filterMarkup(page('<p>x</p>'), SVG, OPTS)).toThrow(FilterRefusal);
  });

  // req: ENF-SEC-004
  it('is idempotent: filtering what was filtered changes nothing', () => {
    for (const text of Object.values(corpus)) {
      const once = filterMarkup(text, XHTML, OPTS);
      expect(filterMarkup(once, XHTML, OPTS)).toBe(once);
    }
  });
});

describe('the CSS of a book', () => {
  // req: ENF-SEC-004
  it('drops @import, a remote url(), expression and behaviours, and keeps the rest', () => {
    const css = `@import url(https://evil.example/a.css);@import "b.css";p{color:red;background:url(https://evil.example/p.png);width:expression(1)}h1{behavior:url(x);-moz-binding:url(y);margin:0}@font-face{font-family:X;src:url(${LOADER})}i{background:url("${LOADER}")}`;
    const out = filterCss(css, OPTS);
    expect(out).not.toMatch(/evil\.example|@import|expression|behavior|binding|b\.css/i);
    expect(out).toContain('color:red');
    expect(out).toContain('margin:0');
    expect(out).toContain(`url(${LOADER})`);
    expect(out).toContain(`url("${LOADER}")`);
  });

  // req: ENF-SEC-004
  it('sees through escapes, comments and cases', () => {
    for (const evil of ['u\\72l(https://evil.example/a)', 'U\\52 L("https://evil.example/a")', '\\75rl(//evil.example/a)', 'url/**/(https://evil.example/a)', '@\\69mport "https://evil.example/a";', '@IMPORT url(//evil.example/a);', 'src("https://evil.example/a")', 'image("https://evil.example/a")', '-webkit-image-set(url(https://evil.example/a) 1x)', 'cross-fade(url(//evil.example/a), none)']) {
      const out = filterCss(`a{b:${evil}} ${evil}`, OPTS);
      expect(out, evil).not.toContain('evil.example');
    }
  });

  // req: ENF-SEC-004
  it('keeps a string that merely contains the word', () => {
    expect(filterCss('q::before{content:"url(a) and @import"}', OPTS)).toContain('content:"url(a) and @import"');
  });
});

describe('the stable steps of a position (CFI)', () => {
  /** The path of an element: its rank among its sibling elements, up to the root. */
  const path = (el: Element): number[] => {
    const steps: number[] = [];
    for (let n: Element | null = el; n && n.parentElement; n = n.parentElement)
      steps.unshift(Array.from(n.parentElement.children).indexOf(n));
    return steps;
  };
  const marked = (doc: Document) => new Map(Array.from(doc.querySelectorAll('[id]')).map((e) => [e.id, path(e)]));

  // req: ENF-SEC-004
  it('test_content_filter_preserves_cfi_steps: an element that goes is replaced at the same rank', () => {
    const src = page(
      '<p id="a">1</p><script>x()</script><iframe src="https://evil.example/"></iframe><div id="b"><form><input/></form><p id="c">2</p><object data="x"></object><p id="d">3</p></div><embed src="x"/><p id="e">4</p>' +
        svg('<g id="g1"/><script>x()</script><foreignObject><p/></foreignObject><g id="g2"/>'),
      '<meta http-equiv="refresh" content="0"/><base href="x"/>'
    );
    const before = marked(parse(src));
    const after = marked(run(src));
    expect([...after.keys()].sort()).toEqual([...before.keys()].sort());
    for (const [id, steps] of before) expect(after.get(id), id).toEqual(steps);
    // the replacement is empty and bare, so a step into it was never a step into the trap
    const body = run(src).querySelector('body')!;
    const inert = body.children[1]!;
    expect(inert.attributes.length).toBe(0);
    expect(inert.childNodes.length).toBe(0);
  });

  // req: ENF-SEC-004
  it('keeps the text nodes beside a removed element where they were', () => {
    const doc = run(page('<p>avant<script>x()</script>après<b>gras</b></p>'));
    const p = doc.querySelector('p')!;
    expect(p.childNodes.length).toBe(4);
    expect(p.textContent).toBe('avantaprèsgras');
    expect(p.children[1]?.localName).toBe('b');
  });

  // req: ENF-SEC-004
  it('writes the version of the filter that produced the DOM', () => {
    expect(CONTENT_FILTER).toBe('content-filter@2');
  });
});

describe('the hooks on the loader', () => {
  const loaderEvents = () => {
    const target = new EventTarget();
    installFilter(target, OPTS);
    const load = (type: string, isScript = false) => {
      const detail: { type: string; isScript: boolean; allow: boolean | Promise<boolean> } = { type, isScript, allow: true };
      target.dispatchEvent(new CustomEvent('load', { detail }));
      return detail;
    };
    const data = async (name: string, type: string, payload: string | Blob | Promise<Blob>) => {
      const detail: { type: string | Promise<string>; data: unknown; name?: string } = { data: payload, type };
      Object.defineProperty(detail, 'name', { value: name });
      target.dispatchEvent(new CustomEvent('data', { detail }));
      return detail;
    };
    return { load, data };
  };

  // req: ENF-SEC-004
  it('never loads a script and never loads a type outside the list', async () => {
    const { load } = loaderEvents();
    expect(await load('text/javascript', true).allow).toBe(false);
    expect(await load('application/xhtml+xml', true).allow).toBe(false);
    expect(await load('application/wasm').allow).toBe(false);
    expect(await load('image/png').allow).toBe(true);
  });

  // req: ENF-SEC-004
  it('fixes the type of the blob from the name, and filters markup before the blob exists', async () => {
    const { data } = loaderEvents();
    const doc = await data('OEBPS/c1.xhtml', 'image/png', page('<p>x</p><script>a()</script>'));
    expect(await doc.type).toBe(XHTML);
    expect(String(await doc.data)).not.toContain('script');
    const sheet = await data('a/s.css', 'text/css', '@import "x.css";p{color:red}');
    expect(await sheet.type).toBe('text/css');
    expect(String(await sheet.data)).not.toContain('@import');
    const script = await data('a/x.js', 'text/javascript', 'alert(1)');
    expect(await script.type).toBe('application/octet-stream');
    const image = await data('a/p.png', 'application/xhtml+xml', Promise.resolve(new Blob([new Uint8Array(4)])));
    expect(await image.type).toBe('image/png');
  });

  // req: ENF-SEC-004
  it('filters an SVG that the OPF called an image (a blob, not a string)', async () => {
    const { data } = loaderEvents();
    const hidden = await data('a/p.svg', 'image/png', Promise.resolve(new Blob([svg('<script>alert(1)</script><rect width="1" height="1"/>')])));
    expect(await hidden.type).toBe(SVG);
    const out = hidden.data instanceof Blob ? await hidden.data.text() : String(await hidden.data);
    expect(out).not.toContain('script');
    expect(out).toContain('rect');
  });

  // req: ENF-SEC-004
  it('refuses a document that the loader re-read as HTML, and an invalid one', async () => {
    const { data } = loaderEvents();
    const reread = await data('c1.xhtml', 'text/html', page('<p>x</p>'));
    await expect(Promise.resolve(reread.data)).rejects.toBeInstanceOf(FilterRefusal);
    const invalid = await data('c2.xhtml', XHTML, '<p>never closed');
    await expect(Promise.resolve(invalid.data)).rejects.toBeInstanceOf(FilterRefusal);
  });
});
