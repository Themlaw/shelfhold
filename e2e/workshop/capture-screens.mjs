#!/usr/bin/env node
// Screenshots of every screen of the SPA and of the reader, on the synthetic base of the workshop
// (`seed.py`), for the visual review: `node workshop/capture-screens.mjs <outdir> [options]`.
//
// The server is the one of the workshop (`npm run test:workshop` starts it; to keep one open, run
// the `webServer` command of `playwright.config.ts` by hand). Nothing is committed: the PNG go to
// `<outdir>`, with `report.json` (what each screen said about itself: scroll sideways, number of
// `h1`, requests refused outside the instance).
//
// What it does for every capture (the faults of the review: a page cut at the first screen, a sticky
// bar repeated in the middle of a long page, a state that was never drawn):
//  - waits for `networkidle` and for the fonts, scrolls the page to its end (lazy images) and back;
//  - for a full-page capture, puts the sticky bars at the top and the fixed ones at the bottom of
//    the document, so that they are drawn once, where they stand at the ends of the page;
//  - forces the states of a screen by intercepting the API: `empty` (every list of the real answer
//    emptied), `loading` (the data held back), `error` (500), `403`, `401`, and `offline`;
//  - draws the reader from the public domain books of `capture-books.mjs`, in each of its states.
//
// Options:
//   --base <url>      the server (default http://localhost:8792)
//   --only a,b        only the screens whose id starts with one of these
//   --sizes d1440,m390  --schemes light,dark
//   --steps screens,themes,search,library,focus,reader,outside   only these parts (to run several at once)
//   --full            every size and scheme for every screen (the default is shorter, for a slow machine)
//   --list            print the screens and exit
import { chromium } from '@playwright/test';
import { mkdirSync, writeFileSync } from 'node:fs';
import { FABLES, epubFiles, plateHtml } from './capture-books.mjs';

const args = process.argv.slice(2);
const flag = (name, fallback) => {
  const at = args.indexOf(`--${name}`);
  return at === -1 ? fallback : args[at + 1];
};
const out = args.find((a, i) => !a.startsWith('--') && !args[i - 1]?.startsWith('--'));
const BASE = flag('base', 'http://localhost:8792');
const ONLY = flag('only', '')?.split(',').filter(Boolean);
const SIZES = {
  d1440: { width: 1440, height: 900 },
  m390: { width: 390, height: 844 }
};
const STEPS = flag('steps', 'screens,themes,search,library,focus,reader,outside').split(',');
const SIZE_NAMES = flag('sizes', 'd1440,m390').split(',');
const SCHEMES = flag('schemes', 'light,dark').split(',');
const THEMES = ['bibliotheque', 'console', 'galerie', 'calme'];
const ACCOUNTS = {
  admin: { username: 'admin', password: 'synthetic-admin-password-1' },
  membre: { username: 'membre', password: 'synthetic-profile-password-1' },
  enfant: { username: 'enfant8', password: 'synthetic-profile-password-1' }
};
const MAX_HEIGHT = 8000;

// ---------------------------------------------------------------------------------------------
// The screens, from the inventory (`INVENTAIRE-FRONT.md`, parts A, B, C): one entry per route of
// the SPA. `states` lists what is forced besides the default state.
// ---------------------------------------------------------------------------------------------
const LIST = ['empty', 'loading', 'error', '403'];
/** @type {{id:string, path:string|((ids:Record<string,string>)=>string|null), role?:string, states?:string[], viewport?:boolean}[]} */
const SCREENS = [
  { id: 'accueil', path: '/', states: LIST },
  { id: 'accueil-membre', path: '/', role: 'membre' },
  { id: 'accueil-enfant', path: '/', role: 'enfant' },
  { id: 'chercher', path: '/chercher', states: ['error', '403'] },
  { id: 'chercher-enfant', path: '/chercher', role: 'enfant' },
  { id: 'bibliotheque', path: '/bibliotheque', states: LIST },
  { id: 'bibliotheque-selections', path: '/bibliotheque/selections', states: LIST },
  { id: 'bibliotheque-membre', path: '/bibliotheque', role: 'membre' },
  { id: 'serie', path: (ids) => (ids.edition ? `/serie/${ids.edition}` : null), states: ['loading', 'error', '403', '404'] },
  { id: 'serie-migrer', path: (ids) => (ids.edition ? `/serie/${ids.edition}/migrer` : null) },
  { id: 'collections', path: '/collections', states: LIST },
  { id: 'collection', path: (ids) => (ids.collection ? `/collections/${ids.collection}` : null), states: ['404'] },
  { id: 'mes-demandes', path: '/mes-demandes', role: 'membre', states: LIST },
  { id: 'mes-demandes-enfant', path: '/mes-demandes', role: 'enfant' },
  { id: 'a-approuver', path: '/a-approuver', states: LIST },
  { id: 'a-approuver-liste', path: '/a-approuver/liste' },
  { id: 'a-classer', path: '/a-classer', states: LIST },
  { id: 'a-ranger', path: '/a-ranger', states: LIST },
  { id: 'a-ranger-meme-oeuvre', path: '/a-ranger/meme-oeuvre', states: ['empty'] },
  { id: 'activite', path: '/activite', states: LIST },
  { id: 'activite-file', path: '/activite/file', states: LIST },
  { id: 'activite-historique', path: '/activite/historique', states: LIST },
  { id: 'appareils', path: '/appareils', states: ['empty', 'error'] },
  { id: 'appareils-jetons', path: '/appareils/jetons', states: ['empty', 'error'] },
  { id: 'compte-passkeys', path: '/compte/passkeys', states: ['empty', 'error'] },
  { id: 'compte-trackers', path: '/compte/trackers', states: ['empty', 'error'] },
  { id: 'corbeille', path: '/corbeille', states: LIST },
  { id: 'sources', path: '/sources', states: LIST },
  { id: 'sante', path: '/sante', states: ['loading', 'error', '403'] },
  { id: 'sante-stockage', path: '/sante/stockage', states: ['error'] },
  { id: 'reglages', path: '/reglages' },
  { id: 'reglages-adoption', path: '/reglages/adoption', states: ['empty', 'error'] },
  { id: 'reglages-apparence', path: '/reglages/apparence' },
  { id: 'reglages-compte-kavita', path: '/reglages/compte-kavita' },
  { id: 'reglages-oidc', path: '/reglages/compte/oidc' },
  { id: 'reglages-destinations', path: '/reglages/destinations', states: ['empty', 'error'] },
  { id: 'reglages-etageres', path: '/reglages/etageres', states: ['empty', 'error', 'loading'] },
  { id: 'reglages-modules', path: '/reglages/modules', states: ['empty', 'error'] },
  { id: 'reglages-notifications', path: '/reglages/notifications' },
  { id: 'reglages-profils', path: '/reglages/profils', states: ['error'] },
  { id: 'reglages-webhooks', path: '/reglages/webhooks', states: ['empty', 'error'] },
  { id: 'assistant-compte', path: '/assistant/compte' },
  { id: 'assistant-dossier', path: '/assistant/dossier' },
  { id: 'assistant-etageres', path: '/assistant/etageres' },
  { id: 'assistant-kavita', path: '/assistant/kavita' },
  { id: 'assistant-fin', path: '/assistant/fin' },
  { id: 'hors-ligne', path: '/hors-ligne' },
  { id: '404', path: '/cette-page-n-existe-pas' },
  { id: '404-serie', path: '/serie/00000000000000000000000000' }
];

// ---------------------------------------------------------------------------------------------
// The browser
// ---------------------------------------------------------------------------------------------
const browser = await chromium.launch();
const report = { base: BASE, shots: [], problems: [] };
const refused = [];
// one sign-in per profile (the hashing of a password is slow): the cookies are handed to every context
const states = {};
let lastShot = Date.now();

async function context({ size, scheme, theme = 'bibliotheque', role = 'admin', blockWorkers = false, extra = {} }) {
  const ctx = await browser.newContext({
    baseURL: BASE,
    locale: 'fr-FR',
    viewport: SIZES[size],
    colorScheme: scheme,
    isMobile: SIZES[size].width < 600,
    hasTouch: SIZES[size].width < 600,
    serviceWorkers: blockWorkers ? 'block' : 'allow',
    ...(role && states[role] ? { storageState: states[role] } : {}),
    ...extra
  });
  // the fence of the workshop (ENF-PRIV-001): nothing leaves the instance, and what tries is counted
  const origin = new URL(BASE).origin;
  await ctx.route('**/*', (route) => {
    const url = route.request().url();
    if (url.startsWith('data:') || url.startsWith('blob:') || new URL(url).origin === origin) return route.fallback();
    refused.push(url);
    return route.abort();
  });
  if (role && !states[role]) {
    const answer = await ctx.request.post('/api/v1/auth/login', { data: ACCOUNTS[role], timeout: 180000 });
    if (!answer.ok()) throw new Error(`sign in as ${role}: ${answer.status()}`);
    states[role] = await ctx.storageState();
  }
  // the profile's own preference wins over the browser's storage (EF-UX-012): it is answered with
  // the theme and the scheme of the capture, and nothing is written on the server
  await ctx.route('**/api/v1/me/preferences', async (route) => {
    if (route.request().method() !== 'GET') return route.fallback();
    const real = await route.fetch({ timeout: 60000 }).catch(() => null);
    if (!real || !real.ok()) return route.fallback();
    return route.fulfill({ response: real, json: { ...(await real.json()), theme, chosen_theme: theme, color_scheme: scheme } });
  });
  await ctx.addInitScript(
    ([t, s]) => {
      try {
        localStorage.setItem('shelfhold.appearance', JSON.stringify({ theme: t, colorScheme: s }));
      } catch {
        /* storage closed */
      }
    },
    [theme, scheme]
  );
  return ctx;
}

async function discover() {
  const ctx = await context({ size: 'd1440', scheme: 'light' });
  const get = async (path) => {
    const answer = await ctx.request.get(path);
    return answer.ok() ? answer.json() : null;
  };
  const ids = {};
  const works = (await get('/api/v1/works?limit=200'))?.items ?? [];
  const wanted = works.find((w) => w.title === 'Vingt mille lieues sous les mers') ?? works[0];
  ids.edition = wanted?.edition_id;
  ids.collection = (await get('/api/v1/collections'))?.items?.[0]?.id;
  const next = (await get('/api/reader/continue'))?.items?.[0];
  ids.carrier = next?.carrier_id;
  ids.carrierType = next?.carrier_type ?? 'content';
  ids.unit = next?.unit_id;
  await ctx.close();
  return ids;
}

// ---------------------------------------------------------------------------------------------
// Settling and neutralising a page for a full-page capture
// ---------------------------------------------------------------------------------------------
async function settle(page) {
  // the stream of events stays open for ever: `networkidle` is given a short time, then the data is awaited by the pause
  await page.waitForLoadState('networkidle', { timeout: 2000 }).catch(() => {});
  await page.evaluate(() => document.fonts?.ready).catch(() => {});
  // the whole page is walked, so that what is lazy is loaded, then put back at the top
  await page.evaluate(async () => {
    const step = Math.max(200, Math.floor(innerHeight * 0.8));
    for (let y = 0; y < document.documentElement.scrollHeight; y += step) {
      window.scrollTo(0, y);
      await new Promise((r) => setTimeout(r, 60));
    }
    window.scrollTo(0, 0);
  });
  await page.waitForLoadState('networkidle', { timeout: 1500 }).catch(() => {});
  await ready(page);
}

/** A screen is ready when no skeleton is left and its title is there (a title is waited for 10 s at
 * most: some screens have none). A capture taken on a loaded machine is otherwise a page half drawn. */
async function ready(page) {
  await page.waitForFunction(() => !document.querySelector('.skeleton'), null, { timeout: 90000 }).catch(() => {});
  await page.waitForFunction(() => document.querySelector('h1') !== null, null, { timeout: 10000 }).catch(() => {});
  await page.waitForFunction(() => !document.querySelector('.skeleton'), null, { timeout: 5000 }).catch(() => {});
  // the images of the page, covers first: a cover not yet drawn is a beige block
  await page.waitForFunction(() => [...document.images].every((i) => i.complete), null, { timeout: 90000 }).catch(() => {});
  await page.waitForTimeout(400);
}

/** Sticky bars at their place in the flow, fixed ones at the end they hold, so that a full-page
 * capture draws each once (the review's defect m10). Dialogs and overlays are left as they are. */
async function neutralise(page) {
  await page.evaluate(() => {
    const height = document.documentElement.scrollHeight;
    for (const el of document.querySelectorAll('body *')) {
      const style = getComputedStyle(el);
      if (style.position !== 'sticky' && style.position !== 'fixed') continue;
      if (el.closest('[role=dialog], dialog, [aria-modal=true]')) continue;
      const box = el.getBoundingClientRect();
      if (style.position === 'sticky') {
        el.style.setProperty('position', 'static', 'important');
        // a bar that scrolls inside itself (the table of contents) is shown whole
        el.style.setProperty('height', 'auto', 'important');
        el.style.setProperty('max-height', 'none', 'important');
        el.style.setProperty('overflow', 'visible', 'important');
        continue;
      }
      if (box.width > innerWidth * 0.95 && box.height > innerHeight * 0.8) continue; // an overlay
      const atBottom = parseFloat(style.bottom) < parseFloat(style.top) || style.top === 'auto';
      el.style.setProperty('position', 'absolute', 'important');
      if (atBottom) {
        el.style.setProperty('top', `${height - box.height}px`, 'important');
        el.style.setProperty('bottom', 'auto', 'important');
      }
    }
  });
}

async function metrics(page) {
  return page.evaluate(() => {
    const w = document.documentElement.clientWidth;
    const overflow = document.documentElement.scrollWidth - w;
    const bad =
      overflow > 0
        ? [...document.querySelectorAll('body *')]
            .filter((e) => e.getBoundingClientRect().right > w + 0.5)
            .slice(0, 5)
            .map((e) => `${e.tagName}.${e.className?.baseVal ?? e.className} r=${Math.round(e.getBoundingClientRect().right)}`)
        : [];
    return {
      overflow,
      bad,
      h1: document.querySelectorAll('h1').length,
      theme: document.documentElement.dataset.theme,
      title: document.title
    };
  });
}

async function shoot(page, name, { full = true } = {}) {
  const info = await metrics(page);
  let options = { path: `${out}/${name}.png`, timeout: 120000 };
  if (full) {
    await neutralise(page);
    const height = await page.evaluate(() => document.documentElement.scrollHeight);
    const width = page.viewportSize().width;
    options = { ...options, fullPage: true, clip: { x: 0, y: 0, width, height: Math.min(height, MAX_HEIGHT) } };
  }
  await page.screenshot(options);
  report.shots.push({ name, ...info });
  if (info.overflow > 0) report.problems.push(`${name}: scrolls sideways by ${info.overflow}px ${JSON.stringify(info.bad)}`);
  if (full && info.h1 !== 1) report.problems.push(`${name}: ${info.h1} h1`);
  console.log('ok', name, `+${Math.round((Date.now() - lastShot) / 1000)}s`);
  lastShot = Date.now();
}

// ---------------------------------------------------------------------------------------------
// States forced by the network
// ---------------------------------------------------------------------------------------------
// the layout's own calls, and the stream of events (it never ends: it is not a state of a screen)
const LAYOUT_CALLS = /\/api\/v1\/(me|me\/preferences|source|events)(\?.*)?$/;
const isData = (url) => {
  const { pathname } = new URL(url);
  return (pathname.startsWith('/api/v1/') || pathname.startsWith('/api/reader/')) && !LAYOUT_CALLS.test(pathname);
};
const problem = (status, title) => ({
  status,
  contentType: 'application/problem+json',
  body: JSON.stringify({ type: 'about:blank', title, status, detail: title, instance: '/capture' })
});

/** Every list of an answer emptied (the shape stays the real one: `items`, counters, cursors). */
function emptied(value) {
  if (Array.isArray(value)) return [];
  if (value && typeof value === 'object') {
    return Object.fromEntries(
      Object.entries(value).map(([k, v]) => [
        k,
        Array.isArray(v) ? [] : typeof v === 'number' && /^(total|count|.*_count)$/.test(k) ? 0 : /cursor$/.test(k) ? null : v
      ])
    );
  }
  return value;
}

/** Installs the forced state on the page; returns what releases a held request. */
async function force(page, state) {
  let release = () => {};
  const gate = new Promise((resolve) => (release = resolve));
  await page.route('**/api/**', async (route) => {
    const request = route.request();
    if (request.method() !== 'GET' || !isData(request.url())) return route.fallback();
    switch (state) {
      case 'empty': {
        const real = await route.fetch({ timeout: 15000 }).catch(() => null);
        if (!real) return route.fallback();
        const type = real.headers()['content-type'] ?? '';
        if (!real.ok() || !type.includes('json')) return route.fulfill({ response: real });
        return route.fulfill({ response: real, json: emptied(await real.json()) });
      }
      case 'loading':
        await gate;
        return route.abort();
      case 'error':
        return route.fulfill(problem(500, 'Erreur interne du serveur'));
      case '403':
        return route.fulfill(problem(403, 'Accès refusé'));
      case '404':
        return route.fulfill(problem(404, 'Introuvable'));
      case '401':
        return route.fulfill(problem(401, 'Session expirée'));
      default:
        return route.fallback();
    }
  });
  return release;
}

// ---------------------------------------------------------------------------------------------
// Every screen, in its default state and in the forced ones
// ---------------------------------------------------------------------------------------------
const wanted = (id) => !ONLY?.length || ONLY.some((o) => id.startsWith(o));
// The matrix. Whole (`--full`): every size and scheme for the default state, three cells for a forced
// one. Short, the default of a slow machine: every cell for the screens that carry the product
// (KEY), two for the others (desktop light, mobile dark), two for a forced state.
const FULL = args.includes('--full');
const KEY = ['accueil', 'bibliotheque', 'serie', 'chercher', 'collections', 'a-approuver', 'sources', 'sante', 'reglages', 'activite', 'hors-ligne', '404'];
const cells = (states, id = '') => {
  const all = SIZE_NAMES.flatMap((s) => SCHEMES.map((c) => [s, c]));
  const pick = (list) => list.filter(([s, c]) => SIZE_NAMES.includes(s) && SCHEMES.includes(c));
  if (!states) return FULL || KEY.includes(id) ? all : pick([['d1440', 'light'], ['m390', 'dark']]);
  return pick(FULL ? [['d1440', 'light'], ['m390', 'light'], ['m390', 'dark']] : [['d1440', 'light'], ['m390', 'light']]);
};

async function visit(page, path) {
  await page.goto(path, { waitUntil: 'domcontentloaded' }).catch(() => {});
  await settle(page);
}

async function captureScreens(ids) {
  const sessions = new Map();
  const session = async (size, scheme, role, theme) => {
    const key = [size, scheme, role, theme].join('/');
    if (!sessions.has(key)) sessions.set(key, await context({ size, scheme, role, theme, blockWorkers: true }));
    return sessions.get(key);
  };
  for (const screen of SCREENS.filter((s) => wanted(s.id))) {
    const path = typeof screen.path === 'function' ? screen.path(ids) : screen.path;
    if (!path) {
      report.problems.push(`${screen.id}: no id to open it with`);
      continue;
    }
    for (const state of ['default', ...(screen.states ?? [])]) {
      for (const [size, scheme] of cells(state === 'default' ? null : screen.states, screen.id)) {
        const ctx = await session(size, scheme, screen.role ?? 'admin', 'bibliotheque');
        const page = await ctx.newPage();
        const release = state === 'default' ? () => {} : await force(page, state);
        try {
          await page.goto(path, { waitUntil: 'domcontentloaded' }).catch(() => {});
          if (state === 'loading') {
            await page.waitForTimeout(1200);
            await shoot(page, `${screen.id}--loading--${size}--${scheme}`, { full: false });
          } else {
            await settle(page);
            await shoot(page, `${screen.id}${state === 'default' ? '' : `--${state}`}--${size}--${scheme}`);
          }
        } catch (error) {
          report.problems.push(`${screen.id} ${state} ${size} ${scheme}: ${String(error).slice(0, 200)}`);
        } finally {
          release();
          await page.close();
        }
      }
    }
  }
  for (const ctx of sessions.values()) await ctx.close();
}

// ---------------------------------------------------------------------------------------------
// The four themes: home, library and the 404, in every size and scheme
// ---------------------------------------------------------------------------------------------
async function captureThemes() {
  for (const theme of THEMES) {
    for (const size of SIZE_NAMES) {
      for (const scheme of SCHEMES) {
        if (!FULL && size === 'm390' && scheme === 'dark') continue;
        const ctx = await context({ size, scheme, theme, blockWorkers: true });
        for (const [id, path] of [['accueil', '/'], ['bibliotheque', '/bibliotheque'], ['404', '/cette-page-n-existe-pas']]) {
          if (!wanted(`theme-${id}`)) continue;
          const page = await ctx.newPage();
          await visit(page, path);
          const applied = await page.evaluate(() => document.documentElement.dataset.theme);
          if (applied !== theme) report.problems.push(`theme-${id}: asked ${theme}, the page says ${applied}`);
          await shoot(page, `theme-${id}--${theme}--${size}--${scheme}`);
          await page.close();
        }
        await ctx.close();
      }
    }
  }
}

// ---------------------------------------------------------------------------------------------
// Search with results and the AddSheet; the library filtered to nothing; the offline screens
// ---------------------------------------------------------------------------------------------
const FOUND = {
  action: null,
  formats: ['epub', 'cbz'],
  state: 'results',
  next_cursor: null,
  items: [
    {
      appended: false,
      group_id: 'g1',
      title: 'Les Fables de La Fontaine',
      external_ids: [],
      editions: [
        { format: 'epub', remote_id: 'fables-epub', source_key: 'catalogue-public', title: 'Fables, livres I à VI' },
        { format: 'cbz', remote_id: 'fables-cbz', source_key: 'catalogue-public', title: 'Fables illustrées' }
      ],
      editions_by_format: {
        epub: [{ format: 'epub', remote_id: 'fables-epub', source_key: 'catalogue-public', title: 'Fables, livres I à VI' }],
        cbz: [{ format: 'cbz', remote_id: 'fables-cbz', source_key: 'catalogue-public', title: 'Fables illustrées' }]
      }
    },
    {
      appended: false,
      group_id: 'g2',
      title: 'Le Tour du monde en quatre-vingts jours',
      external_ids: [],
      editions: [{ format: 'epub', remote_id: 'verne-80', source_key: 'catalogue-public', title: 'Le Tour du monde en quatre-vingts jours' }],
      editions_by_format: { epub: [{ format: 'epub', remote_id: 'verne-80', source_key: 'catalogue-public', title: 'Le Tour du monde en quatre-vingts jours' }] }
    }
  ],
  searched_in: [{ code: null, format: 'epub', name: 'Catalogue public', returned: 2, source_key: 'catalogue-public', status: 'ok', trust: null }]
};

async function captureSearch() {
  if (!wanted('chercher')) return;
  for (const size of SIZE_NAMES) {
    for (const scheme of SCHEMES) {
      const ctx = await context({ size, scheme, blockWorkers: true });
      const page = await ctx.newPage();
      await page.route('**/api/v1/search*', (route) => route.fulfill({ json: FOUND }));
      await visit(page, '/chercher');
      await page.locator('main input').first().fill('fables');
      await page.keyboard.press('Enter');
      await page.waitForTimeout(800);
      await settle(page);
      await shoot(page, `chercher-resultats--${size}--${scheme}`);
      // the AddSheet: the first button that adds an edition
      const add = page.getByRole('button', { name: /ajouter|add/i }).first();
      if (await add.count()) {
        await add.click();
        await page.waitForTimeout(500);
        await shoot(page, `chercher-addsheet--${size}--${scheme}`, { full: false });
      } else report.problems.push('chercher-addsheet: no add button found');
      await ctx.close();
    }
  }
  // the search that finds nothing, and the one that cannot be asked
  for (const [id, body] of [['aucun-resultat', { ...FOUND, state: 'empty', items: [], action: 'request', searched_in: FOUND.searched_in }], ['indisponible', { ...FOUND, state: 'unavailable', items: [] }]]) {
    const ctx = await context({ size: 'd1440', scheme: 'light', blockWorkers: true });
    const page = await ctx.newPage();
    await page.route('**/api/v1/search*', (route) => route.fulfill({ json: body }));
    await visit(page, '/chercher');
    await page.locator('main input').first().fill('zzzz');
    await page.keyboard.press('Enter');
    await page.waitForTimeout(600);
    await shoot(page, `chercher-${id}--d1440--light`);
    await ctx.close();
  }
}

async function captureLibraryFilter() {
  if (!wanted('bibliotheque')) return;
  for (const size of SIZE_NAMES) {
    const ctx = await context({ size, scheme: 'light', blockWorkers: true });
    const page = await ctx.newPage();
    // a filter that matches nothing: the list answers empty once a series or a shelf is asked for
    await page.route('**/api/v1/works*', async (route) => {
      const url = new URL(route.request().url());
      if (!url.searchParams.has('series_id') && !url.searchParams.has('shelf_id')) return route.fallback();
      const real = await route.fetch();
      return route.fulfill({ response: real, json: emptied(await real.json()) });
    });
    await visit(page, '/bibliotheque');
    const select = page.locator('select').first();
    if (await select.count()) {
      const options = await select.locator('option').allTextContents();
      if (options.length > 1) await select.selectOption({ index: 1 });
      await page.waitForTimeout(800);
      await settle(page);
    } else report.problems.push('bibliotheque-filtre: no select found');
    await shoot(page, `bibliotheque-filtre-vide--${size}--light`);
    await ctx.close();
  }
}

async function captureKeyboardFocus() {
  if (!wanted('focus')) return;
  for (const [id, path] of [['accueil', '/'], ['bibliotheque', '/bibliotheque'], ['reglages', '/reglages'], ['chercher', '/chercher']]) {
    for (const scheme of SCHEMES) {
      const ctx = await context({ size: 'd1440', scheme, blockWorkers: true });
      const page = await ctx.newPage();
      await visit(page, path);
      for (const presses of [1, 4]) {
        await page.evaluate(() => document.activeElement?.blur());
        for (let i = 0; i < presses; i++) await page.keyboard.press('Tab');
        await page.waitForTimeout(200);
        await shoot(page, `focus-${id}-tab${presses}--d1440--${scheme}`, { full: false });
      }
      await ctx.close();
    }
  }
}

// ---------------------------------------------------------------------------------------------
// The reader
// ---------------------------------------------------------------------------------------------
const TICKET = '/content/TICKET/';
let browserPages;

/** A page of the book of plates, drawn by the browser itself and kept as a PNG. */
async function plate(n, count, width, height) {
  browserPages ??= await browser.newContext({ viewport: { width, height }, deviceScaleFactor: 1 });
  const page = await browserPages.newPage();
  await page.setViewportSize({ width, height });
  await page.setContent(plateHtml(n, count, width, height));
  const png = await page.screenshot({ type: 'png', timeout: 180000 });
  await page.close();
  return png;
}

function publication(ids, kind, extra = {}) {
  const base = {
    carrier_id: ids.carrier,
    carrier_type: 'content',
    expires_at: '2099-01-01T00:00:00Z',
    language: 'fr',
    number: '2',
    scope: 'full',
    title: kind === 'epub' ? 'Fables' : 'Fables illustrées',
    reading_direction: 'ltr'
  };
  if (kind === 'epub') {
    const names = Object.keys(epubFiles()).filter((n) => n.endsWith('.xhtml') && n !== 'OEBPS/nav.xhtml');
    return {
      ...base,
      kind: 'epub',
      pages: [],
      package: `${TICKET}OEBPS/content.opf`,
      spine: names.map((n, i) => ({ index: i + 1, href: `${TICKET}${n}`, media_type: 'application/xhtml+xml' })),
      ...extra
    };
  }
  return {
    ...base,
    kind: 'pages',
    package: null,
    spine: [],
    pages: Array.from({ length: PAGES }, (_, i) => ({ index: i + 1, href: `${TICKET}p${i + 1}.png`, media_type: 'image/png' })),
    ...extra
  };
}
const PAGES = 12;
const PLATES = new Map(); // the plates drawn once, for every scene

/** The routes that make a book of this kind readable. */
async function serveBook(page, ids, kind, extra = {}, size = 'd1440') {
  const { width, height } = kind === 'webtoon' ? { width: 800, height: 2400 } : { width: 800, height: 1200 };
  const cache = PLATES;
  await page.route('**/api/reader/*/*/publication*', (route) =>
    route.fulfill({ json: publication(ids, kind === 'epub' ? 'epub' : 'pages', extra) })
  );
  await page.route(`**${TICKET}**`, async (route) => {
    const name = decodeURIComponent(new URL(route.request().url()).pathname.replace(TICKET, ''));
    if (kind === 'epub') {
      const body = epubFiles()[name];
      return body === undefined ? route.fulfill({ status: 404, body: '' }) : route.fulfill({ body, contentType: 'application/octet-stream' });
    }
    const n = Number(/^p(\d+)\.png$/.exec(name)?.[1]);
    if (!n) return route.fulfill({ status: 404, body: '' });
    const key = `${n}/${width}x${height}`;
    if (!cache.has(key)) cache.set(key, await plate(n, PAGES, width, height));
    return route.fulfill({ body: cache.get(key), contentType: 'image/png' });
  });
}

async function openReader(page, ids, query = '') {
  await page.goto(`/lire/${ids.carrierType}/${ids.carrier}${query}`, { waitUntil: 'domcontentloaded' });
  await page.waitForLoadState('networkidle', { timeout: 2000 }).catch(() => {});
  await page.waitForTimeout(1200);
}

/** Waits until what the reader draws is drawn: every image of the page loaded (a book of pages) or
 * the document of the chapter in its frame (an EPUB). A capture taken before is a blank page. */
async function drawn(page) {
  await page
    .waitForFunction(
      () => {
        const imgs = [...document.querySelectorAll('.pages img')];
        if (imgs.length) return imgs.every((i) => i.complete && i.naturalWidth > 0);
        return document.querySelector('.pages foliate-view, .pages iframe') !== null || document.querySelector('.state, [role=alert]') !== null;
      },
      null,
      { timeout: 45000 }
    )
    .catch(() => {});
  await page.waitForTimeout(500);
}

const READER_SCENES = [
  { id: 'pages', run: async (page, ids) => { await serveBook(page, ids, 'pages'); await openReader(page, ids); } },
  { id: 'pages-page-4', run: async (page, ids) => { await serveBook(page, ids, 'pages'); await openReader(page, ids); for (let i = 0; i < 3; i++) { await drawn(page); await page.keyboard.press('ArrowRight'); await page.waitForTimeout(200); } } },
  { id: 'pages-rtl', run: async (page, ids) => { await serveBook(page, ids, 'pages', { reading_direction: 'rtl' }); await openReader(page, ids); } },
  { id: 'pages-fin', run: async (page, ids) => { await serveBook(page, ids, 'pages'); await openReader(page, ids); for (let i = 0; i < 40 && !(await page.locator('[data-reader-end]').count()); i++) { await drawn(page); await page.keyboard.press('ArrowRight'); await page.waitForTimeout(150); } } },
  { id: 'webtoon', run: async (page, ids) => { await serveBook(page, ids, 'webtoon', { reading_direction: 'webtoon' }); await openReader(page, ids); } },
  { id: 'epub', run: async (page, ids) => { await serveBook(page, ids, 'epub'); await openReader(page, ids); await page.waitForTimeout(1500); } },
  { id: 'extrait', run: async (page, ids) => { await serveBook(page, ids, 'pages', { scope: 'excerpt' }); await openReader(page, ids, '?scope=excerpt'); } },
  { id: 'reglages', run: async (page, ids) => { await serveBook(page, ids, 'pages'); await openReader(page, ids); await page.getByRole('button', { name: 'Réglages de lecture' }).click(); await page.waitForTimeout(500); } },
  { id: 'portee', run: async (page, ids) => { await serveBook(page, ids, 'pages'); await openReader(page, ids); await page.getByRole('button', { name: 'Réglages de lecture' }).click(); await page.getByLabel('Pleine largeur').check({ force: true }); await page.waitForTimeout(500); } },
  { id: 'aide', run: async (page, ids) => { await serveBook(page, ids, 'pages'); await openReader(page, ids); await page.keyboard.press('?'); await page.waitForTimeout(500); } },
  { id: 'chargement', run: async (page, ids) => { await page.route('**/api/reader/*/*/publication*', () => {}); await openReader(page, ids); } },
  { id: 'introuvable', run: async (page, ids) => { await page.route('**/api/reader/*/*/publication*', (r) => r.fulfill(problem(409, 'Aucun exemplaire lisible'))); await openReader(page, ids); } },
  { id: 'refuse-403', run: async (page, ids) => { await page.route('**/api/reader/*/*/publication*', (r) => r.fulfill(problem(403, 'Accès refusé'))); await openReader(page, ids); } },
  { id: 'refuse-401', run: async (page, ids) => { await page.route('**/api/reader/*/*/publication*', (r) => r.fulfill(problem(401, 'Session expirée'))); await openReader(page, ids); } },
  { id: 'serveur-500', run: async (page, ids) => { await page.route('**/api/reader/*/*/publication*', (r) => r.fulfill(problem(500, 'Erreur interne'))); await openReader(page, ids); } },
  { id: 'format-non-lu', run: async (page, ids) => { await serveBook(page, ids, 'pages', { pages: [{ index: 1, href: `${TICKET}p1.tif`, media_type: 'image/tiff' }] }); await openReader(page, ids); } },
  { id: 'navigateur-non-pris-en-charge', ua: 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/60.0.3112.113 Safari/537.36', run: async (page, ids) => { await serveBook(page, ids, 'epub'); await openReader(page, ids); } },
  {
    id: 'politique-refusee',
    run: async (page, ids) => {
      // a page served without the policy of the reader is not the page that was audited
      await serveBook(page, ids, 'pages');
      await page.route('**/lire/**', async (route) => {
        const real = await route.fetch();
        const headers = Object.fromEntries(Object.entries(real.headers()).filter(([k]) => k !== 'content-security-policy'));
        return route.fulfill({ response: real, headers });
      });
      await openReader(page, ids);
    }
  },
  { id: 'sans-reseau-sans-copie', run: async (page, ids) => { await page.route('**/api/reader/*/*/publication*', (r) => r.abort()); await openReader(page, ids); } }
];

async function captureReader(ids) {
  if (!ids.carrier) {
    report.problems.push('reader: no carrier to open (is the base seeded?)');
    return;
  }
  for (const scene of READER_SCENES.filter((s) => wanted(`lecteur-${s.id}`))) {
    for (const size of SIZE_NAMES) {
      for (const scheme of SCHEMES) {
        const ctx = await context({ size, scheme, blockWorkers: true, extra: scene.ua ? { userAgent: scene.ua } : {} });
        const page = await ctx.newPage();
        try {
          await scene.run(page, ids);
          await drawn(page);
          await shoot(page, `lecteur-${scene.id}--${size}--${scheme}`, { full: false });
        } catch (error) {
          report.problems.push(`lecteur-${scene.id} ${size} ${scheme}: ${String(error).slice(0, 200)}`);
        }
        await ctx.close();
      }
    }
  }
  await captureReaderOffline(ids);
}

/** The copy kept on the device, then the reader and the screens offline: the real stores (IndexedDB,
 * Cache Storage) of a real browser. */
async function captureReaderOffline(ids) {
  if (!wanted('lecteur-hors-ligne') && !wanted('hors-ligne') && !wanted('bandeau')) return;
  for (const size of SIZE_NAMES) {
    for (const scheme of SCHEMES) {
      const ctx = await context({ size, scheme, blockWorkers: false });
      const page = await ctx.newPage();
      await serveBook(page, ids, 'pages');
      await openReader(page, ids);
      const keep = page.getByRole('button', { name: /Lire hors ligne/ });
      if (!(await keep.count())) {
        report.problems.push(`lecteur-hors-ligne ${size} ${scheme}: no « Lire hors ligne » button`);
        await ctx.close();
        continue;
      }
      await keep.first().click();
      await page.waitForTimeout(2500);
      await shoot(page, `lecteur-copie-gardee--${size}--${scheme}`, { full: false });
      // the SPA, online, with a copy kept: the list of the copies
      await page.unroute('**/api/reader/*/*/publication*');
      await page.unroute(`**${TICKET}**`);
      await visit(page, '/hors-ligne');
      await shoot(page, `hors-ligne-avec-copies--${size}--${scheme}`);
      await visit(page, '/');
      await ctx.setOffline(true);
      await page.waitForTimeout(1000);
      await shoot(page, `bandeau-hors-ligne--${size}--${scheme}`, { full: false });
      await visit(page, '/hors-ligne');
      await shoot(page, `hors-ligne-reseau-coupe--${size}--${scheme}`);
      // the reader opened with no network: the kept copy answers
      await page.goto(`/lire/${ids.carrierType}/${ids.carrier}`, { waitUntil: 'domcontentloaded' }).catch(() => {});
      await page.waitForTimeout(2000);
      await shoot(page, `lecteur-hors-ligne-copie--${size}--${scheme}`, { full: false });
      await ctx.close();
    }
  }
}

// ---------------------------------------------------------------------------------------------
// Screens served by the backend, 401 on the SPA
// ---------------------------------------------------------------------------------------------
async function captureOutsideSpa() {
  if (!wanted('login') && !wanted('session')) return;
  for (const size of SIZE_NAMES) {
    for (const scheme of SCHEMES) {
      const ctx = await context({ size, scheme, role: null, blockWorkers: true });
      const page = await ctx.newPage();
      if (wanted('login')) {
        await visit(page, '/login');
        await shoot(page, `login--${size}--${scheme}`);
        await visit(page, '/lite');
        await shoot(page, `lite-hors-https--${size}--${scheme}`);
      }
      await ctx.close();
    }
  }
  if (wanted('session')) {
    // the session the server no longer knows: the screen says so instead of showing an empty page
    for (const size of SIZE_NAMES) {
      const ctx = await context({ size, scheme: 'light', blockWorkers: true });
      const page = await ctx.newPage();
      await page.route('**/api/v1/me', (r) => r.fulfill(problem(401, 'Session expirée')));
      await visit(page, '/bibliotheque');
      await shoot(page, `session-perdue--${size}--light`);
      await ctx.close();
    }
  }
}

// ---------------------------------------------------------------------------------------------
if (args.includes('--list')) {
  for (const s of SCREENS) console.log(s.id, typeof s.path === 'string' ? s.path : '(from the base)', (s.states ?? []).join(','));
  console.log(READER_SCENES.map((s) => `lecteur-${s.id}`).join('\n'));
  await browser.close();
  process.exit(0);
}
if (!out) {
  console.error('usage: node workshop/capture-screens.mjs <outdir> [--base url] [--only a,b] [--sizes d1440,m390] [--schemes light,dark] [--list]');
  process.exit(2);
}
mkdirSync(out, { recursive: true });
const ids = await discover();
console.log('ids', JSON.stringify(ids));
const PARTS = {
  screens: () => captureScreens(ids),
  themes: captureThemes,
  search: captureSearch,
  library: captureLibraryFilter,
  focus: captureKeyboardFocus,
  reader: () => captureReader(ids),
  outside: captureOutsideSpa
};
for (const name of STEPS) {
  try {
    await PARTS[name]();
  } catch (error) {
    report.problems.push(`${name}: ${String(error).slice(0, 300)}`);
  }
}
if (refused.length) report.problems.push(`requests refused outside the instance: ${[...new Set(refused)].join(', ')}`);
writeFileSync(`${out}/report-${STEPS.join('+')}.json`, JSON.stringify(report, null, 2));
console.log(`${report.shots.length} captures, ${report.problems.length} problems (report-*.json)`);
await browserPages?.close();
await browser.close();
