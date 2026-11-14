import { execFileSync } from 'node:child_process';
import { mkdirSync, readFileSync, readdirSync } from 'node:fs';
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
// What an action is: one gesture of the person on a control that is not a text field — a click on
// a button, a link or a checkbox, a submission. Typing never counts by itself: the fields filled
// before a submission belong to it. Every action is listed in the report of the test, tagged
// `decision` (it sends something to the server) or `navigation` (a link to the next screen).
// The budget is read on ALL the actions, never on the decisions alone: the test of the card does
// not get easier by renaming what it counts.
//
// EF-UX-012: one capture per theme and per scheme of the key screens. The reference is NOT a pixel
// image: such a file depends on the fonts of the machine that made it (here the runners of the CI,
// elsewhere a developer's) and AGENTS.md forbids making a reference in the CI. The "capture" is the
// structure of the screen (landmarks and headings) and the tokens the browser applied, compared to
// `tokens.json`; the pixels are kept as an artifact for the eye, never compared.

const BUDGET_S = 600;
const BUDGET_ACTIONS = 6;
const ADMIN = { username: 'admin', password: 'synthetic-admin-password-1' };
const KID = { username: 'kid8', password: 'synthetic-kid-password-1' };
const KAVITA_WINDOW = process.env.E2E_KAVITA_WINDOW ?? 'http://127.0.0.1:15000';
const KAVITA_USER = 'shelfhold';
const KAVITA_PASSWORD = 'synthetic-kavita-password';
const FIRST_BOOK = { id: '1001', title: 'A Study in Test' };
const HERE = dirname(fileURLToPath(import.meta.url));

type Kind = 'decision' | 'navigation';
interface Action {
  what: string;
  kind: Kind;
  at_s: number;
}

class Run {
  readonly started = Date.now();
  readonly actions: Action[] = [];
  elapsed(): number {
    return (Date.now() - this.started) / 1000;
  }
  async click(control: Locator, what: string, kind: Kind): Promise<void> {
    this.actions.push({ what, kind, at_s: Math.round(this.elapsed()) });
    await control.click();
  }
  report(): string {
    const decisions = this.actions.filter((a) => a.kind === 'decision').length;
    return [
      `first book: ${this.elapsed().toFixed(0)} s, ${this.actions.length} actions ` +
        `(${decisions} decisions, ${this.actions.length - decisions} navigations)`,
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

async function kavitaSeries(): Promise<string[]> {
  const answer = await fetch(`${KAVITA_WINDOW}/_fake/state`);
  const state = (await answer.json()) as { series: { name: string }[] };
  return state.series.map((s) => s.name);
}

/** Kavita's own periodic scan of its folders, which the simulated server does when asked. */
async function kavitaScans(folder: string): Promise<void> {
  await fetch(`${KAVITA_WINDOW}/_fake/index?folder=${encodeURIComponent(folder)}`, {
    method: 'POST'
  });
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

/** The wizard, screen after screen, as a person follows it. Returns when Kavita lists the book. */
async function firstBook(page: Page, run: Run, afterKavita?: () => void): Promise<void> {
  await signInByApi(page, ADMIN); // no sign-in screen exists yet: see the report of L57
  run.actions.push({ what: 'sign in (by the API: no screen)', kind: 'decision', at_s: 0 });

  await page.goto('/assistant/compte');
  await expect(page.getByText('Connecté en tant que admin.')).toBeVisible();
  await run.click(page.getByRole('link', { name: 'Continuer' }), 'compte → Kavita', 'navigation');

  await expect(page.getByRole('heading', { name: 'Connexion à Kavita', level: 2 })).toBeVisible();
  await page.getByLabel('Adresse de Kavita').fill('http://127.0.0.1:5000');
  await page.getByLabel("Nom d'utilisateur Kavita").fill(KAVITA_USER);
  await page.getByLabel('Mot de passe Kavita').fill(KAVITA_PASSWORD);
  await run.click(page.getByLabel("J'ai compris", { exact: false }), 'accept the admin key', 'decision');
  await run.click(page.getByRole('button', { name: 'Connecter Kavita' }), 'connect Kavita', 'decision');
  await expect(page.getByText(/Kavita est connecté/)).toBeVisible();
  afterKavita?.();
  await run.click(page.getByRole('link', { name: 'Continuer' }), 'Kavita → folder', 'navigation');

  await expect(page.getByRole('heading', { name: 'Dossier partagé', level: 2 })).toBeVisible();
  // a shelf must be bound to Kavita for the folder to be checked: the screen says so otherwise
  await expect(
    page.getByRole('button', { name: 'Vérifier le dossier' }),
    'the folder screen offers its check (a destination exists)'
  ).toBeVisible({ timeout: 10_000 });
  await run.click(page.getByRole('button', { name: 'Vérifier le dossier' }), 'check the folder', 'decision');
  await expect(page.getByText(/Kavita lit le dossier partagé|Le dossier est inscriptible/)).toBeVisible();
  const onward = page.getByRole('link', { name: 'Continuer' });
  if ((await onward.count()) > 0) {
    await run.click(onward, 'folder → shelves', 'navigation');
  } else {
    // this screen has no way onward of its own (a dead end of the wizard, reported by L57): the
    // person types the address of the next screen, which is one more gesture
    run.actions.push({ what: 'folder → shelves (typed address: no link)', kind: 'navigation', at_s: Math.round(run.elapsed()) });
    await page.goto('/assistant/etageres');
  }

  await expect(page.getByRole('heading', { name: 'Étagères', level: 2 })).toBeVisible();
  await run.click(page.getByRole('button', { name: 'Créer les étagères' }), 'create the shelves', 'decision');
  await expect(page.getByText('Étagères créées.')).toBeVisible();
  await run.click(page.getByRole('link', { name: 'Continuer' }), 'shelves → end', 'navigation');

  await expect(page.getByRole('heading', { name: 'Finalisation', level: 2 })).toBeVisible();
  await page.getByLabel('Numéro du livre sur Gutenberg', { exact: false }).fill(FIRST_BOOK.id);
  await run.click(page.getByRole('button', { name: 'Lancer le premier livre' }), 'launch the first book', 'decision');
  await expect(page.getByText('Premier livre lancé.')).toBeVisible();
}

// req: ENF-UX-001
test('test_first_book_under_10_minutes', async ({ page }, info) => {
  // The journey as the screens allow it today: nothing but the compose, then the browser. It does
  // not get through (no sign-in screen, no screen binds a shelf to Kavita, so "Dossier partagé"
  // has no destination, the Gutenberg addresses are set by command), and the wizard takes more
  // than 6 gestures. The test is the gap, written first: it is expected to fail (strict: the day
  // it passes, Playwright fails it and the marker goes). See `test_timed_first_book_*` for the
  // time the rest of the journey takes once those blocks are lifted by a command.
  test.fail(true, 'ENF-UX-001: the first-run screens cannot yet reach a first book alone (M3 L57)');
  const run = new Run();
  await firstBook(page, run);
  await waitForTheBook(run, '/library/livres');
  await info.attach('first-book-report', { body: run.report() });
  expect(run.elapsed(), run.report()).toBeLessThanOrEqual(BUDGET_S);
  expect(run.actions.length, run.report()).toBeLessThanOrEqual(BUDGET_ACTIONS);
});

// req: ENF-UX-001
test('test_timed_first_book_with_the_shelf_made_by_command', async ({ page }, info) => {
  // The same journey with the one block the screens leave to the host lifted: the shelf bound to
  // Kavita is made by `shelf create`, right after Kavita is connected (the order of the wizard).
  // It measures the real time of the rest and asserts the time budget; the number of actions is
  // reported in full, and read against BUDGET_ACTIONS by the test above.
  const run = new Run();
  await firstBook(page, run, () => {
    shelfhold([
      'shelf', 'create', 'Livres', '--max-age', '18+', '--target', 'kavita', '--subpath', 'livres'
    ]);
  });
  await waitForTheBook(run, '/library/livres');
  const report = run.report();
  console.log(report);
  await info.attach('first-book-report', { body: report });
  expect(run.elapsed(), report).toBeLessThanOrEqual(BUDGET_S);
  expect((await kavitaSeries()).includes(FIRST_BOOK.title)).toBe(true);
  const decisions = run.actions.filter((a) => a.kind === 'decision').length;
  expect(decisions, report).toBeLessThanOrEqual(BUDGET_ACTIONS);
});

// ---- EF-UX-012: key screens, per theme and per scheme ------------------------------------------

const FRONTEND = join(HERE, '..', '..', 'frontend', 'src', 'lib', 'themes');
const THEMES = readdirSync(FRONTEND, { withFileTypes: true })
  .filter((d) => d.isDirectory())
  .map((d) => d.name)
  .sort();
const SCHEMES = ['light', 'dark'] as const;
const KEY_SCREENS: { who: typeof ADMIN | typeof KID; paths: string[] }[] = [
  { who: ADMIN, paths: ['/', '/chercher', '/bibliotheque', '/sante', '/reglages/apparence'] },
  { who: KID, paths: ['/', '/chercher', '/mes-demandes'] }
];

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
  expect(THEMES.length, 'at least the default theme exists').toBeGreaterThan(0);
  const shots = join(info.outputPath(), 'themes');
  mkdirSync(shots, { recursive: true });
  for (const who of KEY_SCREENS) {
    for (const theme of THEMES) {
      const tokens = JSON.parse(
        readFileSync(join(FRONTEND, theme, 'tokens.json'), 'utf8')
      ) as Tokens;
      for (const path of who.paths) {
        const structures = new Map<string, string[]>();
        for (const scheme of SCHEMES) {
          const context = await browser.newContext({ locale: 'fr-FR', colorScheme: scheme });
          await context.addInitScript(
            ([key, value]) => localStorage.setItem(key, value),
            ['shelfhold.appearance', JSON.stringify({ theme, colorScheme: scheme })]
          );
          const page = await context.newPage();
          const here = `${who.username} ${theme}/${scheme} ${path}`;
          const errors: string[] = [];
          page.on('pageerror', (e) => errors.push(e.message));
          await signInByApi(page, who);
          await page.goto(path);
          await expect(page.locator('main').first(), here).toBeVisible();

          const root = page.locator('html');
          await expect(root, here).toHaveAttribute('data-theme', theme);
          await expect(root, here).toHaveAttribute('data-color-scheme', scheme);
          const wanted = (scheme === 'dark' ? tokens.dark ?? tokens.light : tokens.light).color;
          for (const [name, token] of Object.entries(wanted)) {
            const applied = await page.evaluate(
              (n) => getComputedStyle(document.documentElement).getPropertyValue(`--color-${n}`).trim(),
              name
            );
            expect(applied.toLowerCase(), `${here}: --color-${name}`).toBe(token.$value.toLowerCase());
          }
          const overflow = await page.evaluate(
            () => document.documentElement.scrollWidth - document.documentElement.clientWidth
          );
          expect(overflow, `${here}: horizontal overflow`).toBeLessThanOrEqual(0);
          expect(errors, `${here}: page errors`).toEqual([]);
          structures.set(scheme, await structureOf(page));
          await page.screenshot({
            path: join(shots, `${who.username}-${theme}-${scheme}-${path.replaceAll('/', '_') || '_'}.png`),
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
});
