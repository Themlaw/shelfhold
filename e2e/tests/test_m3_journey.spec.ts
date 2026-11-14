import { execFileSync } from 'node:child_process';
import { mkdirSync, readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { expect, test, type Locator, type Page } from '@playwright/test';

// M3 L57: the journeys a browser plays on the IMAGE of the release (not on a local server).
// They are not run by `e2e-front` (playwright.config.ts ignores this file): the pytest wrapper of
// `test_m3_journey.py` brings the compose stack up (image under test, simulated Kavita, simulated
// sources, nothing leaving the network namespace), prepares what the administrator types on the
// host, then runs this file with `playwright.image.config.ts`.
//
// ENF-UX-001: "first 10 minutes" — a new installation to the first Gutenberg book listed by Kavita
// in at most 10 minutes (BUDGET_S) and at most 6 actions (BUDGET_ACTIONS).
//
// What an action is (decision M3, L57 "définition d'une action"): one gesture of the person on a
// control that is not a text field — a click on a button, a link or a checkbox, a submission.
// Typing never counts by itself: the fields filled before a submission belong to it. Opening an
// address counts for nothing. Every action is listed in the report of the test, tagged `decision`
// (it sends something to the server), `navigation` (a link to the next screen) or `host` (a
// command typed on the machine: the only one the journey allows is `docker compose up`, which is
// the install itself and is not counted). The budget is read on ALL of them: the interface and the
// host together, no more than 6.
//
// The journey starts at `/`, on a new instance: the app leads to the wizard by itself (nothing to
// type), the account is made on its first screen with the secret that the compose holds, and each
// screen with nothing left to ask moves on by itself (the wizard waits a moment so that what was
// done can be read: that is no gesture, and the wait is inside the time measured).
//
// Two things belong to the harness, not to the person, and are not counted:
// - the two addresses of the simulated books project (catalogue and mirror) exist only in this
//   stack, so the stack gives them to the plugin itself (`fakes/m3/serve_with_sources.py`); a real
//   install talks to the real project and sets nothing;
// - the turn of the scheduler (`jobs run export.run`, then the scan of Kavita), nudged by hand so
//   that the wait for the first book does not depend on the period of the scheduler.
//
// EF-UX-012: one capture per theme and per scheme of the key screens. The reference is NOT a pixel
// image: such a file depends on the fonts of the machine that made it (here the runners of the CI,
// elsewhere a developer's) and AGENTS.md forbids making a reference in the CI. The "capture" is the
// structure of the screen (landmarks and headings) and the tokens the browser applied, compared to
// a reference COMMITTED in `references/key-screens.json` (and to `tokens.json`); the pixels are kept
// as an artifact for the eye, never compared. The reference is made by an explicit command that
// refuses to run in the CI: see `e2e/README.md`.

const BUDGET_S = 600;
const BUDGET_ACTIONS = 6;
const ADMIN = { username: 'admin', password: 'synthetic-admin-password-1' };
// the value of SHELFHOLD_SETUP_TOKEN in `e2e/m3.yaml`
const SETUP_TOKEN = 'synthetic-setup-token-0123456789abcdef';
const KID = { username: 'kid8', password: 'synthetic-kid-password-1' };
// the simulated Kavita listens on the loopback of the stack: its window is read from inside
const KAVITA_WINDOW = 'http://127.0.0.1:5000';
const KAVITA_USER = 'shelfhold';
const KAVITA_PASSWORD = 'synthetic-kavita-password';
const FIRST_BOOK = { id: '1001', title: 'A Study in Test' };
const HERE = dirname(fileURLToPath(import.meta.url));

type Kind = 'decision' | 'navigation' | 'host';
interface Action {
  what: string;
  kind: Kind;
  at_s: number;
}

class Run {
  readonly started = Date.now();
  readonly actions: Action[] = [];
  /** A command typed on the host: one gesture, counted. */
  host(what: string): void {
    this.actions.push({ what: `host: ${what}`, kind: 'host', at_s: Math.round(this.elapsed()) });
  }
  elapsed(): number {
    return (Date.now() - this.started) / 1000;
  }
  async click(control: Locator, what: string, kind: Kind): Promise<void> {
    this.actions.push({ what, kind, at_s: Math.round(this.elapsed()) });
    await control.click();
  }
  report(): string {
    const count = (kind: Kind) => this.actions.filter((a) => a.kind === kind).length;
    return [
      `first book: ${this.elapsed().toFixed(0)} s, ${this.actions.length} actions ` +
        `(${count('decision')} decisions, ${count('navigation')} navigations, ` +
        `${count('host')} host commands)`,
      ...this.actions.map((a, i) => `  ${i + 1}. [${a.kind}] ${a.what} (t+${a.at_s} s)`)
    ].join('\n');
  }
}

/** What the administrator types on the host: the same `shelfhold` as in production. */
function compose(args: string[]): string {
  const base = JSON.parse(process.env.E2E_COMPOSE ?? '[]') as string[];
  return execFileSync('docker', ['compose', ...base, ...args], {
    encoding: 'utf8',
    timeout: 180_000
  });
}

function shelfhold(args: string[]): string {
  return compose(['exec', '-T', 'shelfhold', 'shelfhold', ...args]);
}

const FAKE_GET =
  'import urllib.request as u,sys; sys.stdout.write(u.urlopen(sys.argv[1], timeout=5).read().decode())';
const FAKE_POST =
  "import urllib.request as u,sys; u.urlopen(u.Request(sys.argv[1], data=b'{}', method='POST'), timeout=5)";

async function kavitaSeries(): Promise<string[]> {
  const state = JSON.parse(
    compose(['exec', '-T', 'fake-kavita', 'python', '-c', FAKE_GET, `${KAVITA_WINDOW}/_fake/state`])
  ) as { series: { name: string }[] };
  return state.series.map((s) => s.name);
}

/** Kavita's own periodic scan of its folders, which the simulated server does when asked. */
async function kavitaScans(folder: string): Promise<void> {
  compose([
    'exec', '-T', 'fake-kavita', 'python', '-c', FAKE_POST,
    `${KAVITA_WINDOW}/_fake/index?folder=${encodeURIComponent(folder)}`
  ]);
}

/** The scheduler's turn, asked by hand (the journeys of M1 and M2 do the same): export, then scan. */
async function nudgeExport(folder: string): Promise<void> {
  try {
    shelfhold(['jobs', 'run', 'export.run']);
  } catch {
    // the server was busy with its own write: the next turn asks again
  }
  await kavitaScans(folder);
}

async function signInByApi(page: Page, who: { username: string; password: string }): Promise<void> {
  const answer = await page.request.post('/api/v1/auth/login', { data: who });
  expect(answer.status(), `sign in as ${who.username}`).toBe(200);
}

/** Wait for the first book to be listed by Kavita, nudging the export as the scheduler would. */
async function waitForTheBook(run: Run, folder: string): Promise<void> {
  const deadline = Date.now() + (BUDGET_S + 120) * 1000; // past the budget the assertion speaks
  while (Date.now() < deadline) {
    await nudgeExport(folder);
    if ((await kavitaSeries()).includes(FIRST_BOOK.title)) return;
    await new Promise((resolve) => setTimeout(resolve, 5000));
  }
  throw new Error(`Kavita never listed ${FIRST_BOOK.title}: ${(await kavitaSeries()).join(', ')}`);
}

/** The wizard, screen after screen, as a person follows it. Returns when the first book is launched. */
async function firstBook(page: Page, run: Run): Promise<void> {
  // the address of the instance, nothing else: a new instance leads to the wizard by itself
  await page.goto('/');
  await expect(page).toHaveURL(/\/assistant\/compte$/);
  await expect(page.getByRole('heading', { name: 'Votre compte', level: 2 })).toBeVisible();
  await page.getByLabel("Nom d'utilisateur").fill(ADMIN.username);
  await page.getByLabel('Mot de passe').fill(ADMIN.password);
  await page.getByLabel("Secret d'installation").fill(SETUP_TOKEN); // read in the compose file
  await run.click(page.getByRole('button', { name: 'Créer le compte' }), 'create the account (signs in)', 'decision');
  await expect(page.getByText(`Connecté en tant que ${ADMIN.username}.`)).toBeVisible();

  // the account screen has nothing to ask of the administrator: it moves on by itself
  await expect(page.getByRole('heading', { name: 'Connexion à Kavita', level: 2 })).toBeVisible();
  await page.getByLabel('Adresse de Kavita').fill('http://127.0.0.1:5000');
  await page.getByLabel("Nom d'utilisateur Kavita").fill(KAVITA_USER);
  await page.getByLabel('Mot de passe Kavita').fill(KAVITA_PASSWORD);
  // the simulated Kavita sees the whole volume at the same path: the map the administrator gives
  await page.getByLabel('Chemins des dossiers (avancé)').fill('/library=/library');
  await run.click(page.getByLabel("J'ai compris", { exact: false }), 'accept the admin key', 'decision');
  await run.click(page.getByRole('button', { name: 'Connecter Kavita' }), 'connect Kavita', 'decision');
  await expect(page.getByText(/Kavita est connecté/)).toBeVisible();

  await expect(page.getByRole('heading', { name: 'Dossier partagé', level: 2 })).toBeVisible();
  await run.click(
    page.getByRole('button', { name: "Créer l'étagère et la lier à Kavita" }),
    'create the shelf linked to Kavita',
    'decision'
  );
  await expect(page.getByText('Étagère créée et liée à Kavita.')).toBeVisible();

  await expect(page.getByRole('heading', { name: 'Étagères', level: 2 })).toBeVisible();
  await run.click(page.getByRole('button', { name: 'Créer les étagères' }), 'create the shelves', 'decision');
  await expect(page.getByText('Étagères créées.')).toBeVisible();

  await expect(page.getByRole('heading', { name: 'Finalisation', level: 2 })).toBeVisible();
  await page.getByLabel('Numéro du livre sur Gutenberg', { exact: false }).fill(FIRST_BOOK.id);
  await run.click(page.getByRole('button', { name: 'Lancer le premier livre' }), 'launch the first book', 'decision');
  await expect(page.getByText('Premier livre lancé.')).toBeVisible();
}

// req: ENF-UX-001
test('test_first_book_under_10_minutes', async ({ page }, info) => {
  // A new installation: `docker compose up` (the compose holds the setup secret), then the
  // browser opens `/`. The administrator makes the account, goes through the screens, makes the
  // shelf linked to Kavita, launches a Gutenberg book, and Kavita lists it: within BUDGET_S seconds
  // and BUDGET_ACTIONS actions, every click and submission counted (decisions and navigations
  // alike; a screen that moves on by itself is no gesture) plus any command typed on the host.
  const run = new Run();
  await firstBook(page, run);
  await waitForTheBook(run, '/library/livres');
  const report = run.report();
  console.log(report);
  await info.attach('first-book-report', { body: report });
  expect((await kavitaSeries()).includes(FIRST_BOOK.title), report).toBe(true);
  expect(run.elapsed(), report).toBeLessThanOrEqual(BUDGET_S);
  expect(run.actions.length, report).toBeLessThanOrEqual(BUDGET_ACTIONS);
});

// ---- EF-UX-012: key screens, per theme and per scheme ------------------------------------------

const FRONTEND = join(HERE, '..', '..', 'frontend', 'src', 'lib', 'themes');
const THEMES = readdirSync(FRONTEND, { withFileTypes: true })
  .filter((d) => d.isDirectory())
  .map((d) => d.name)
  .sort();
const SCHEMES = ['light', 'dark'] as const;
// the series sheet is the one of a series the wrapper made known (its edition, `E2E_EDITION_ID`)
const EDITION = process.env.E2E_EDITION_ID ?? '';
const SERIES_SHEET = '/serie/{edition}';
const KEY_SCREENS: { who: typeof ADMIN | typeof KID; paths: string[] }[] = [
  {
    who: ADMIN,
    paths: [
      '/', '/chercher', '/bibliotheque', SERIES_SHEET, '/assistant/kavita', '/sante',
      '/reglages/apparence'
    ]
  },
  { who: KID, paths: ['/', '/chercher', '/mes-demandes'] }
];

// The committed reference: per screen, per theme and per scheme, the tokens the browser applied and
// the structure of the page. Made by `E2E_UPDATE_REFERENCE=1` (see `e2e/README.md`), never by the CI.
const REFERENCE = join(HERE, 'references', 'key-screens.json');
interface Captured {
  tokens: Record<string, string>;
  structure: string[];
}
const UPDATING = process.env.E2E_UPDATE_REFERENCE === '1';

interface Tokens {
  light: { color: Record<string, { $value: string }> };
  dark?: { color: Record<string, { $value: string }> };
}

/** The structure of a screen: its landmarks and headings, in the order of the page. */
async function structureOf(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const out: string[] = [];
    document
      .querySelectorAll('header, nav, main, footer, aside, h1, h2, h3, [role="alert"], [role="status"]')
      .forEach((el) => {
        const tag = el.getAttribute('role') ?? el.tagName.toLowerCase();
        out.push(/^h\d$/.test(tag) ? `${tag}: ${(el.textContent ?? '').trim()}` : tag);
      });
    return out;
  });
}

// req: EF-UX-012
test('test_key_screens_snapshot_per_theme', async ({ browser }, info) => {
  test.setTimeout(240_000);
  expect(
    !(UPDATING && process.env.CI),
    'the reference of the screens is never made in the CI (AGENTS.md): see e2e/README.md'
  ).toBe(true);
  expect(EDITION, 'the wrapper gives the edition of the series sheet').not.toBe('');
  const captured: Record<string, Captured> = {};
  expect(THEMES.length, 'at least the default theme exists').toBeGreaterThan(0);
  const shots = join(info.outputPath(), 'themes');
  mkdirSync(shots, { recursive: true });
  for (const { who, paths } of KEY_SCREENS) {
    for (const theme of THEMES) {
      const tokens = JSON.parse(
        readFileSync(join(FRONTEND, theme, 'tokens.json'), 'utf8')
      ) as Tokens;
      for (const path of paths) {
        const structures = new Map<string, string[]>();
        const screen = path;
        const url = path === SERIES_SHEET ? `/serie/${EDITION}` : path;
        for (const scheme of SCHEMES) {
          const context = await browser.newContext({ locale: 'fr-FR', colorScheme: scheme });
          const page = await context.newPage();
          const here = `${who.username} ${theme}/${scheme} ${path}`;
          const errors: string[] = [];
          page.on('pageerror', (e) => errors.push(e.message));
          await signInByApi(page, who);
          // the choice is the person's own, kept by the server (it overrides what the browser
          // remembers): theme and scheme by the preference route, as the appearance screen sends it
          const csrf = (await context.cookies()).find((c) => c.name.endsWith('csrf'))?.value ?? '';
          const chosen = await page.request.patch('/api/v1/me/preferences', {
            data: { theme, color_scheme: scheme },
            headers: { 'X-CSRF-Token': csrf }
          });
          expect(chosen.status(), `${here}: preferences`).toBe(200);
          await page.goto(url);
          await expect(page.locator('main').first(), here).toBeVisible();

          const root = page.locator('html');
          await expect(root, here).toHaveAttribute('data-theme', theme);
          await expect(root, here).toHaveAttribute('data-color-scheme', scheme);
          const wanted = (scheme === 'dark' ? tokens.dark ?? tokens.light : tokens.light).color;
          const applied: Record<string, string> = {};
          for (const [name, token] of Object.entries(wanted)) {
            applied[name] = (
              await page.evaluate(
                (n) => getComputedStyle(document.documentElement).getPropertyValue(`--color-${n}`).trim(),
                name
              )
            ).toLowerCase();
            expect(applied[name], `${here}: --color-${name}`).toBe(token.$value.toLowerCase());
          }
          const overflow = await page.evaluate(
            () => document.documentElement.scrollWidth - document.documentElement.clientWidth
          );
          expect(overflow, `${here}: horizontal overflow`).toBeLessThanOrEqual(0);
          expect(errors, `${here}: page errors`).toEqual([]);
          const structure = await structureOf(page);
          structures.set(scheme, structure);
          captured[`${who.username} ${theme}/${scheme} ${screen}`] = { tokens: applied, structure };
          await page.screenshot({
            path: join(shots, `${who.username}-${theme}-${scheme}-${screen.replaceAll('/', '_').replaceAll(/[{}]/g, '') || '_'}.png`),
            fullPage: true
          });
          await context.close();
        }
        // the scheme changes the colours, never the screen: the same landmarks and headings
        expect(structures.get('dark'), `${who.username} ${theme} ${path}: structure per scheme`).toEqual(
          structures.get('light')
        );
      }
    }
  }
  if (UPDATING) {
    mkdirSync(dirname(REFERENCE), { recursive: true });
    writeFileSync(REFERENCE, `${JSON.stringify(captured, null, 2)}\n`);
    console.log(`reference written: ${REFERENCE} (${Object.keys(captured).length} screens)`);
    return;
  }
  // compared to the reference committed with the code: a screen that changed shape, or a token
  // that drifted, is a difference to read and to decide (the reference is made again on purpose)
  const reference = JSON.parse(readFileSync(REFERENCE, 'utf8')) as Record<string, Captured>;
  expect(Object.keys(captured).sort(), 'the screens of the reference').toEqual(
    Object.keys(reference).sort()
  );
  for (const [key, now] of Object.entries(captured)) {
    expect(now.tokens, `${key}: tokens`).toEqual(reference[key].tokens);
    expect(now.structure, `${key}: structure`).toEqual(reference[key].structure);
  }
});
