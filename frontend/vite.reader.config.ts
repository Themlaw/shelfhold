// The build of the reader (`/lire`, ADR-0055): an HTML entry of its own, outside the SvelteKit
// router, so that its policy is the only one that applies (a route of the SPA would carry the
// SPA's `<meta>` policy next to it). It is built after the SPA, into `build/reader-entry/`, and the
// server writes its header from the very page this produces (`reader_csp`).
import { svelte } from '@sveltejs/vite-plugin-svelte';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import type { Plugin } from 'vite';
import { defineConfig } from 'vite';
import { auditReaderPage } from './scripts/csp.ts';

const BASE = '/reader-entry/';
const root = fileURLToPath(new URL('.', import.meta.url));
const TEMPLATE = fileURLToPath(new URL('./reader.html', import.meta.url));

// The formats of foliate that M5 does not read. `view.js` imports them dynamically and Vite would
// make a chunk of each: they are resolved to a module that throws instead, so that no PDF, MOBI,
// FB2, comic-book, zip (the entries are read one by one from the content route), speech or search
// code is ever shipped, with the submodule left as it is.
const NOT_READ = /\/vendor\/foliate-js\/(pdf|mobi|fb2|comic-book|tts|search|footnotes|quote-image|opds|dict|reader)\.js$|\/vendor\/foliate-js\/ui\/|\/vendor\/foliate-js\/vendor\/(pdfjs|fflate|zip)(\.js|\/)/;
const STUB = '\0shelfhold-foliate-not-read';

function foliateNotRead(): Plugin {
  return {
    name: 'shelfhold-foliate-not-read',
    enforce: 'pre',
    async resolveId(source, importer, options) {
      if (source === STUB) return STUB;
      if (!importer || !importer.includes('/vendor/foliate-js/')) return null;
      const resolved = await this.resolve(source, importer, { ...options, skipSelf: true });
      if (resolved && NOT_READ.test(resolved.id.replaceAll('\\', '/'))) return STUB;
      return null;
    },
    load(id) {
      if (id !== STUB) return null;
      return 'const refuse = () => { throw new Error("this format is not read by the reader of Shelfhold"); };\nexport const makePDF = refuse, isMOBI = refuse, MOBI = refuse, makeFB2 = refuse, makeComicBook = refuse, TTS = refuse, searchMatcher = refuse, FootnoteHandler = refuse;\nexport default refuse;\n';
    }
  };
}

// The page that is served: one inline script (hashed by the policy) that adds the entry as a script
// element. Under `'strict-dynamic'` a script that a trusted script creates is trusted, and so is
// everything it imports; a bare `import()` from the inline script is NOT (a parser-inserted script
// passes its metadata to the request, and the browser refuses it: measured on Chromium). No
// `<script src>` in the page, no `modulepreload` (`'self'` is ignored and they would be refused).
function readerPage(): Plugin {
  return {
    name: 'shelfhold-reader-page',
    enforce: 'post', // after the style sheet is emitted
    generateBundle(_, bundle) {
      const entry = Object.values(bundle).find((chunk) => chunk.type === 'chunk' && chunk.isEntry);
      if (!entry) throw new Error('the reader entry was not produced');
      // the page is `reader.html` with, added here and nowhere else, its style sheets and its boot
      // script: the template holds no script, so there is nothing to strip
      const sheets = Object.keys(bundle)
        .filter((name) => name.endsWith('.css'))
        .map((name) => `<link rel="stylesheet" href="${BASE}${name}">`);
      const boot = `document.head.append(Object.assign(document.createElement("script"),{type:"module",src:${JSON.stringify(BASE + entry.fileName)}}));`;
      const html = readFileSync(TEMPLATE, 'utf8').replace('</head>', `${sheets.join('')}<script>${boot}</script></head>`);
      auditReaderPage(html);
      this.emitFile({ type: 'asset', fileName: 'reader.html', source: html });
    }
  };
}

export default defineConfig({
  root,
  base: BASE,
  publicDir: false,
  plugins: [svelte(), foliateNotRead(), readerPage()],
  build: {
    outDir: 'build/reader-entry',
    emptyOutDir: true,
    modulePreload: false,
    cssCodeSplit: false,
    assetsInlineLimit: 0, // no `data:` URL: the policy admits none
    rollupOptions: { input: { reader: fileURLToPath(new URL('./src/reader/main.ts', import.meta.url)) } }
  }
});
