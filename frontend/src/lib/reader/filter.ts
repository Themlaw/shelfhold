// The content filter of the EPUB reader (ADR-0055, "Filtre de contenu (client)"): a whitelist,
// applied to every EPUB whatever its trust level, hooked to the `load` and `data` events of the
// foliate `Loader` before any `blob:` exists. It is the second barrier of the invariant "no script
// of a book ever runs" (the first is the CSP a `blob:` document inherits), and the only one for a
// script that trusted code would move (`'strict-dynamic'`). It shares its list with the server
// neutralizer (`xhtml-neutralizer@v2`) on purpose: they back each other.
//
// Four rules hold everywhere:
// - nothing is kept that is not on a list (elements, URL schemes, types); what is not known goes;
// - what goes from a body is replaced by an empty, bare element of the same rank, so the steps of
//   a CFI do not move (ADR-0055, "Stabilité du CFI");
// - a document that does not parse is refused, never parsed again as HTML;
// - the type of a `blob:` comes from the name the loader read, never from the OPF.

/** The version of this filter, written in the locator of every position (ADR-0055). */
export const CONTENT_FILTER = 'content-filter@2';

const XHTML = 'application/xhtml+xml';
const SVG = 'image/svg+xml';
const CSS = 'text/css';
const OCTET = 'application/octet-stream';
const NS_HTML = 'http://www.w3.org/1999/xhtml';
const NS_SVG = 'http://www.w3.org/2000/svg';
const NS_MATH = 'http://www.w3.org/1998/Math/MathML';
const NS_XLINK = 'http://www.w3.org/1999/xlink';

export class FilterRefusal extends Error {
  constructor(reason: string) {
    super(`content filter: ${reason}`);
    this.name = 'FilterRefusal';
  }
}

export interface FilterOptions {
  /** The origin of the instance (`location.origin`): the `blob:` of the loader are `blob:<origin>/…`. */
  origin: string;
}

// ---------------------------------------------------------------------------------------------
// Types

const BY_EXTENSION: Record<string, string> = {
  xhtml: XHTML,
  xht: XHTML,
  html: XHTML,
  htm: XHTML,
  svg: SVG,
  css: CSS,
  png: 'image/png',
  jpg: 'image/jpeg',
  jpeg: 'image/jpeg',
  gif: 'image/gif',
  webp: 'image/webp',
  avif: 'image/avif',
  woff: 'font/woff',
  woff2: 'font/woff2',
  ttf: 'font/ttf',
  otf: 'font/otf'
};

/** The type of the `blob:` of `name`: from its last extension in a whitelist, else opaque. */
export function mediaTypeFor(name: string): string {
  const last = name.split(/[/\\]/).pop() ?? '';
  const dot = last.lastIndexOf('.');
  if (dot < 0) return OCTET;
  return BY_EXTENSION[last.slice(dot + 1).toLowerCase()] ?? OCTET;
}

// what an OPF may declare for something the loader is allowed to read at all
const LOADABLE = new Set([
  XHTML,
  SVG,
  CSS,
  'image/png',
  'image/jpeg',
  'image/jpg',
  'image/gif',
  'image/webp',
  'image/avif',
  'font/woff',
  'font/woff2',
  'font/ttf',
  'font/otf',
  'font/sfnt',
  'application/font-woff',
  'application/font-woff2',
  'application/font-sfnt',
  'application/x-font-ttf',
  'application/x-font-truetype',
  'application/x-font-opentype',
  'application/vnd.ms-opentype'
]);

/** May the loader read an item the OPF declares with this type? A script never, nor an unknown type. */
export function isLoadable(declared: string, isScript: boolean): boolean {
  if (isScript) return false;
  return LOADABLE.has(declared.trim().toLowerCase());
}

// ---------------------------------------------------------------------------------------------
// URLs

/** An attribute value without what a browser throws away before reading the scheme. */
// eslint-disable-next-line no-control-regex -- the control characters are what a browser drops before it reads a scheme
const bare = (value: string): string => value.replace(/[\u0000-\u0020\u007f-\u009f\u200b-\u200f\u2028\u2029\ufeff]/g, '');

const isLoaderUrl = (value: string, origin: string): boolean => bare(value).startsWith(`blob:${origin}/`) && !/[\s"'<>]/.test(value.trim());

/** The only links foliate intercepts: a document of the book or the web, to be confirmed apart. */
function isLink(value: string, origin: string): boolean {
  const v = bare(value);
  if (v === '') return false;
  if (v.startsWith('#')) return true;
  const scheme = /^([a-z][a-z0-9+.-]*):/i.exec(v);
  if (scheme) {
    const name = scheme[1]!.toLowerCase();
    if (name !== 'http' && name !== 'https') return false;
    try {
      // an address of the instance (its host, whatever the port or the scheme) is never a link of a book
      return new URL(v).hostname !== new URL(origin).hostname;
    } catch {
      return false;
    }
  }
  if (v.startsWith('/') || v.startsWith('\\')) return false; // root- and scheme-relative: the instance
  return true; // a relative reference inside the book, resolved by foliate against the section
}

// attributes that carry an address, whatever the element
const URL_ATTRS = new Set([
  'href',
  'src',
  'srcset',
  'poster',
  'data',
  'action',
  'background',
  'cite',
  'formaction',
  'ping',
  'longdesc',
  'usemap',
  'manifest',
  'codebase',
  'icon',
  'profile',
  'lowsrc',
  'dynsrc',
  'imagesrcset',
  'xlink:href',
  'xml:base'
]);
const ALWAYS_OUT = new Set(['srcdoc', 'http-equiv', 'formaction', 'ping', 'xml:base', 'nonce', 'is', 'manifest']);

// ---------------------------------------------------------------------------------------------
// CSS

const FETCHING = new Set(['image-set', '-webkit-image-set', 'image', 'src', 'cross-fade', '-webkit-cross-fade', 'element', '-moz-element', 'paint', 'expression', 'attr']);
const DROPPED_PROPERTIES = new Set(['behavior', '-moz-binding', '-ms-behavior', 'binding']);

function decodeIdent(raw: string): string {
  return raw.replace(/\\([0-9a-f]{1,6})\s?|\\([^\n])/gi, (_, hex: string | undefined, plain: string | undefined) => {
    if (hex !== undefined) {
      const code = Number.parseInt(hex, 16);
      return code > 0 && code <= 0x10ffff ? String.fromCodePoint(code) : '�';
    }
    return plain ?? '';
  });
}

/** The end of the identifier that starts at `from`: an escape is a unit (`\\52 ` takes its space). */
function identEnd(css: string, from: number): number {
  let i = from;
  while (i < css.length) {
    const c = css[i]!;
    if (c === '\\') {
      const hex = /^[0-9a-f]{1,6}[ \t\n\r\f]?/i.exec(css.slice(i + 1, i + 8));
      i += hex ? 1 + hex[0].length : 2;
    } else if (IDENT_CHAR.test(c)) i += 1;
    else break;
  }
  return Math.min(i, css.length);
}

// eslint-disable-next-line no-control-regex -- ASCII boundary of an identifier
const IDENT_START = /[a-z_\-\\]|[^\u0000-\u007f]/i;
// eslint-disable-next-line no-control-regex -- ASCII boundary of an identifier
const IDENT_CHAR = /[a-z0-9_\-\\]|[^\u0000-\u007f]/i;

/** The CSS of a book without a fetch it did not bring, an `@import`, a script hook or a comment. */
export function filterCss(css: string, options: FilterOptions): string {
  const out: string[] = [];
  const n = css.length;
  let i = 0;

  const readString = (quote: string): string => {
    let j = i + 1;
    while (j < n && css[j] !== quote) j += css[j] === '\\' ? 2 : 1;
    const text = css.slice(i, Math.min(j + 1, n));
    i = Math.min(j + 1, n);
    return text;
  };
  const skipBlock = (): void => {
    // from an opening parenthesis to its match, strings and escapes respected
    let depth = 0;
    while (i < n) {
      const c = css[i]!;
      if (c === '"' || c === "'") {
        readString(c);
        continue;
      }
      if (c === '\\') {
        i += 2;
        continue;
      }
      i += 1;
      if (c === '(') depth += 1;
      if (c === ')' && --depth <= 0) return;
    }
  };
  const skipStatement = (): void => {
    // to the `;` that ends a rule or declaration, or the `}` that closes its block (kept)
    let depth = 0;
    while (i < n) {
      const c = css[i]!;
      if (c === '"' || c === "'") {
        readString(c);
        continue;
      }
      if (c === '\\') {
        i += 2;
        continue;
      }
      if (c === '(' || c === '[') depth += 1;
      if (c === ')' || c === ']') depth = Math.max(0, depth - 1);
      if (c === ';' && depth === 0) {
        i += 1;
        return;
      }
      if (c === '{' && depth === 0) {
        // a block: drop it whole
        let braces = 0;
        while (i < n) {
          const d = css[i]!;
          if (d === '"' || d === "'") {
            readString(d);
            continue;
          }
          i += d === '\\' ? 2 : 1;
          if (d === '{') braces += 1;
          if (d === '}' && --braces <= 0) return;
        }
        return;
      }
      if (c === '}' && depth === 0) return;
      i += 1;
    }
  };

  while (i < n) {
    const c = css[i]!;
    if (c === '/' && css[i + 1] === '*') {
      const end = css.indexOf('*/', i + 2);
      i = end < 0 ? n : end + 2;
      out.push(' '); // a comment between the letters of a word is never a way to write it
      continue;
    }
    if (c === '"' || c === "'") {
      out.push(readString(c));
      continue;
    }
    if (c === '<' && css.startsWith('<!--', i)) {
      i += 4;
      continue;
    }
    if (c === '@') {
      const start = i;
      i += 1;
      const wordEnd = identEnd(css, i);
      const name = decodeIdent(css.slice(i, wordEnd)).toLowerCase();
      i = wordEnd;
      if (name === 'import' || name === 'charset' || name === 'document' || name === '-moz-document') {
        i = start;
        skipStatement();
        continue;
      }
      out.push(css.slice(start, i));
      continue;
    }
    if (IDENT_START.test(c)) {
      const start = i;
      i = identEnd(css, i);
      const raw = css.slice(start, i);
      const name = decodeIdent(raw).toLowerCase();
      // `url/**/(`: a comment between the name and the parenthesis does not end the function
      let k = i;
      while (k < n && (/\s/.test(css[k]!) || (css[k] === '/' && css[k + 1] === '*'))) {
        if (css[k] === '/') {
          const end = css.indexOf('*/', k + 2);
          k = end < 0 ? n : end + 2;
        } else k += 1;
      }
      const call = css[k] === '(';
      if (call && name === 'url') {
        i = k;
        const open = i;
        skipBlock();
        const body = css.slice(open + 1, Math.max(open + 1, i - 1)).trim();
        const unquoted = body.replace(/^(["'])(.*)\1$/s, '$2');
        const quote = /^(["'])/.exec(body)?.[1] ?? '';
        const target = decodeIdent(unquoted).trim();
        if (isLoaderUrl(target, options.origin)) out.push(`url(${quote}${target}${quote})`);
        else if (target.startsWith('#')) out.push(`url(${quote}${target}${quote})`);
        else out.push('none');
        continue;
      }
      if (call && FETCHING.has(name)) {
        i = k;
        skipBlock();
        out.push('none');
        continue;
      }
      // a property that hooks a script or a binding: the whole declaration goes
      let after = i;
      while (after < n && /\s/.test(css[after]!)) after += 1;
      if (DROPPED_PROPERTIES.has(name) && css[after] === ':') {
        i = start;
        skipStatement();
        continue;
      }
      out.push(raw);
      continue;
    }
    out.push(c);
    i += 1;
  }
  return out.join('');
}

// ---------------------------------------------------------------------------------------------
// Markup

const HTML_ELEMENTS = new Set(
  (
    'html head title body meta link style a abbr address article aside b bdi bdo blockquote br caption cite code col colgroup data dd del details dfn div dl dt em figcaption figure footer h1 h2 h3 h4 h5 h6 header hgroup hr i img ins kbd li main mark nav ol p picture pre q rp rt ruby s samp section small source span strong sub summary sup table tbody td tfoot th thead time tr u ul var wbr audio video track center font big tt strike acronym'
  ).split(' ')
);
const SVG_ELEMENTS = new Set(
  (
    'svg g defs desc title metadata symbol use image a switch style text tspan textpath rect circle ellipse line polyline polygon path marker pattern clippath mask lineargradient radialgradient stop filter fegaussianblur feoffset feblend fecolormatrix fecomposite feflood femerge femergenode fedropshadow femorphology feturbulence fedisplacementmap feimage fecomponenttransfer fefunca fefuncb fefuncg fefuncr feconvolvematrix fediffuselighting fespecularlighting fedistantlight fepointlight fespotlight fetile view'
  ).split(' ')
);
const MATH_ELEMENTS = new Set(
  (
    'math mi mn mo ms mtext mspace mrow mfrac msqrt mroot mstyle merror mpadded mphantom mfenced menclose msub msup msubsup munder mover munderover mmultiscripts mprescripts none mtable mtr mtd mlabeledtr mglyph'
  ).split(' ')
);
const INERT = { [NS_HTML]: 'span', [NS_SVG]: 'g', [NS_MATH]: 'mrow' } as const;

// the allowed shape of a name of attribute (a colon only for the known prefixes)
const ATTR_NAME = /^(?:[a-z_][\w.-]*|(?:xml|xlink|epub|xmlns|xsi|aria|data)(?::[\w.-]+)?)$/i;

function isUrlAttr(name: string): boolean {
  return URL_ATTRS.has(name.toLowerCase());
}

function cleanAttributes(el: Element, options: FilterOptions): void {
  const name = el.localName.toLowerCase();
  const ns = el.namespaceURI;
  for (const attr of Array.from(el.attributes)) {
    const key = attr.name.toLowerCase();
    const value = attr.value;
    const drop = () => el.removeAttributeNode(attr);
    if (!ATTR_NAME.test(attr.name) || key.startsWith('on') || ALWAYS_OUT.has(key)) {
      drop();
      continue;
    }
    if (key.startsWith('xmlns')) continue; // a namespace declaration: it binds, it fetches nothing
    if (key === 'style') {
      el.setAttribute(attr.name, filterCss(value, options));
      continue;
    }
    if (isUrlAttr(key)) {
      if (key === 'srcset' || key === 'imagesrcset') {
        const candidates = value.split(',').map((c) => c.trim().split(/\s+/)[0] ?? '');
        if (!candidates.every((c) => isLoaderUrl(c, options.origin))) drop();
        continue;
      }
      const link = ns === NS_HTML && name === 'a' && key === 'href';
      const ok = isLoaderUrl(value, options.origin) || bare(value).startsWith('#') || (link && isLink(value, options.origin));
      if (!ok) drop();
      continue;
    }
    // a presentation attribute of SVG or a value of MathML parsed as CSS: a fetch hides in there
    if (/url\s*\(|image-set|expression|@import|\\/i.test(value) && (ns === NS_SVG || ns === NS_MATH)) {
      el.setAttribute(attr.name, filterCss(value, options));
    }
  }
}

function inertFor(doc: Document, el: Element): Element {
  const ns = (el.namespaceURI ?? NS_HTML) as keyof typeof INERT;
  return doc.createElementNS(ns in INERT ? ns : NS_HTML, INERT[ns] ?? 'span');
}

function allowed(el: Element, options: FilterOptions): boolean {
  const ns = el.namespaceURI;
  const name = el.localName.toLowerCase();
  if (ns === NS_HTML) {
    if (!HTML_ELEMENTS.has(name)) return false;
    if (name === 'link') {
      const rel = (el.getAttribute('rel') ?? '').trim().toLowerCase();
      return rel === 'stylesheet' && isLoaderUrl(el.getAttribute('href') ?? '', options.origin);
    }
    if (name === 'meta') return !el.hasAttribute('http-equiv'); // viewport, charset, name/content: text only
    return true;
  }
  if (ns === NS_SVG) {
    if (!SVG_ELEMENTS.has(name)) return false;
    if (name === 'use') {
      const href = el.getAttribute('href') ?? el.getAttributeNS(NS_XLINK, 'href') ?? '';
      return bare(href).startsWith('#') || isLoaderUrl(href, options.origin);
    }
    return true;
  }
  if (ns === NS_MATH) return MATH_ELEMENTS.has(name);
  return false;
}

function walk(doc: Document, parent: Node, options: FilterOptions): void {
  for (const child of Array.from(parent.childNodes)) {
    if (child.nodeType === 1) {
      const el = child as Element;
      if (!allowed(el, options)) {
        parent.replaceChild(inertFor(doc, el), el); // with its content, at the same rank
        continue;
      }
      cleanAttributes(el, options);
      if (el.localName.toLowerCase() === 'style') {
        el.textContent = filterCss(el.textContent ?? '', options);
        continue;
      }
      walk(doc, el, options);
    } else if (child.nodeType === 7) {
      parent.removeChild(child); // a processing instruction: `xml-stylesheet` is a fetch
    } else if (child.nodeType === 10) {
      // a DOCTYPE may carry entities: the document is re-written without one
      parent.removeChild(child);
    }
  }
}

/** Filters an XHTML or SVG document. Throws `FilterRefusal` for what does not parse as the type says. */
export function filterMarkup(text: string, type: string, options: FilterOptions): string {
  if (type !== XHTML && type !== SVG) throw new FilterRefusal(`not a markup type: ${type}`);
  const doc = new DOMParser().parseFromString(text, type);
  const root = doc.documentElement;
  if (!root || doc.querySelector('parsererror') || root.localName.toLowerCase() === 'parsererror') throw new FilterRefusal('the document does not parse');
  const expected = type === XHTML ? NS_HTML : NS_SVG;
  if (root.namespaceURI !== expected || root.localName.toLowerCase() !== (type === XHTML ? 'html' : 'svg')) {
    throw new FilterRefusal('the root of the document is not the one of its type');
  }
  walk(doc, doc, options);
  return new XMLSerializer().serializeToString(doc);
}

// ---------------------------------------------------------------------------------------------
// The hooks on the loader

interface LoadDetail {
  type: string;
  isScript: boolean;
  allow: boolean | Promise<boolean>;
}
interface DataDetail {
  data: unknown;
  type: string | Promise<string>;
  readonly name?: string;
}

async function asText(data: unknown): Promise<string | null> {
  const value = await data;
  if (typeof value === 'string') return value;
  if (value instanceof Blob) return value.text();
  if (value instanceof ArrayBuffer) return new TextDecoder('utf-8').decode(value);
  if (ArrayBuffer.isView(value)) return new TextDecoder('utf-8').decode(value);
  return null;
}

/**
 * Hooks the filter on the events of a foliate `Loader` (`book.transformTarget`): `load` stops a
 * script and a type that is not on the list; `data` fixes the type of the `blob:` from its name and
 * filters markup and CSS before the blob is made. Install it before the first read.
 */
export function installFilter(target: EventTarget, options: FilterOptions): void {
  target.addEventListener('load', (event) => {
    const detail = (event as CustomEvent<LoadDetail>).detail;
    if (!isLoadable(String(detail.type ?? ''), detail.isScript === true)) detail.allow = false;
  });
  target.addEventListener('data', (event) => {
    const detail = (event as CustomEvent<DataDetail>).detail;
    const name = detail.name ?? '';
    const declared = detail.type;
    const final = mediaTypeFor(name);
    const original = detail.data;
    detail.type = final;
    if (final === XHTML || final === SVG || final === CSS) {
      detail.data = (async () => {
        if ((await declared) === 'text/html') throw new FilterRefusal('the loader read the document as HTML: it was not valid XHTML');
        const text = await asText(original);
        if (text === null) throw new FilterRefusal('unreadable document');
        return final === CSS ? filterCss(text, options) : filterMarkup(text, final, options);
      })();
      // a rejected promise nobody reads yet must not be reported as unhandled before the loader awaits it
      (detail.data as Promise<unknown>).catch(() => undefined);
    }
  });
}
