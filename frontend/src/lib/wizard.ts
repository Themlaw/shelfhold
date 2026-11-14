// The first-run wizard (EF-UX-001): at most five screens in all; part 1 is the first three. The
// screens only show what the server answers: every route behind them is the admin's, on a fresh
// session (a stale one asks for the password in place, `ReauthPrompt`).
import { goto } from '$app/navigation';
import type { MessageKey } from '#lib/i18n/index.ts';
import { problemView, type ProblemView } from '#lib/problems.ts';
import { needsReauth, needsSession, problemOf } from '#lib/triage.ts';

export const WIZARD_TOTAL = 5;

export interface WizardStep {
  id: 'compte' | 'kavita' | 'dossier' | 'etageres' | 'fin';
  href: string;
  nameKey: MessageKey;
}

export const WIZARD_STEPS: readonly WizardStep[] = [
  { id: 'compte', href: '/assistant/compte', nameKey: 'admin.wizard.step.compte' },
  { id: 'kavita', href: '/assistant/kavita', nameKey: 'admin.wizard.step.kavita' },
  { id: 'dossier', href: '/assistant/dossier', nameKey: 'admin.wizard.step.dossier' },
  { id: 'etageres', href: '/assistant/etageres', nameKey: 'admin.wizard.step.etageres' },
  { id: 'fin', href: '/assistant/fin', nameKey: 'admin.wizard.step.fin' }
];

/** The step after `id`, or null on the last one. */
export function nextStep(id: WizardStep['id']): WizardStep | null {
  const at = WIZARD_STEPS.findIndex((s) => s.id === id);
  return WIZARD_STEPS[at + 1] ?? null;
}

/** The free libraries the wizard proposes to open to the family search (D13), by the server's key. */
export const SOURCE_NAMES: Readonly<Record<string, MessageKey>> = {
  gutenberg: 'admin.wizard.sources.name.gutenberg',
  wikisource: 'admin.wizard.sources.name.wikisource',
  standardebooks: 'admin.wizard.sources.name.standardebooks'
};
/** Marked "family" until the admin says otherwise on a fresh instance (D13). */
export const FAMILY_BY_DEFAULT: readonly string[] = ['gutenberg', 'wikisource'];

/** What a screen does with an action that did not go through. */
export interface Outcome {
  reauth: (methods: string[]) => void;
  signedOut: () => void;
  problem: (view: ProblemView) => void;
}

/** Run an action of the wizard; a refusal is handed to the screen as what it is (a stale
 * password asked in place, no session, or the server's own problem). True when it went through. */
export async function attempt(action: () => Promise<void>, outcome: Outcome): Promise<boolean> {
  try {
    await action();
    return true;
  } catch (error) {
    if (needsReauth(error)) outcome.reauth(problemOf(error).reauth_methods ?? ['password']);
    else if (needsSession(error)) outcome.signedOut();
    else outcome.problem(problemView(error));
    return false;
  }
}

/** How long a finished screen stays in front of the admin before the wizard moves on by itself:
 * long enough to read what was done, short enough that the journey does not wait on it. */
export const ADVANCE_MS = 1200;

/** Move to the screen after `id` once `ms` have passed, as nothing is left to ask on this one
 * (the "Continuer" link stays for whoever does not want to wait). Returns what cancels the move:
 * a screen that goes away first must not push the person somewhere they did not choose. */
export function advanceFrom(id: WizardStep['id'], ms: number = ADVANCE_MS): () => void {
  const target = nextStep(id);
  if (!target) return () => {};
  const timer = setTimeout(() => {
    // eslint-disable-next-line svelte/no-navigation-without-resolve -- the app is served at the root of its origin
    void goto(target.href);
  }, ms);
  return () => clearTimeout(timer);
}

export type PathPair = { local: string; remote: string; mode: string };

/** "/library=/books, /other=/other" (the shared folder as seen here = as Kavita sees it) into the
 * map the server takes; null when a pair is not `local=remote` with two absolute paths. An empty
 * text is no map at all: the server's default applies. */
export function parsePathMap(text: string): PathPair[] | null | undefined {
  const items = text.split(',').map((item) => item.trim()).filter(Boolean);
  if (items.length === 0) return undefined;
  const pairs: PathPair[] = [];
  for (const item of items) {
    const [local, remote, ...rest] = item.split('=').map((part) => part.trim());
    if (rest.length > 0 || !local?.startsWith('/') || !remote?.startsWith('/')) return null;
    pairs.push({ local, remote, mode: 'rw' });
  }
  return pairs;
}

/** One plain name for the folder of a shelf under the shared one: never a path (the server holds
 * the same rule; here it only spares the round trip). */
export const SHELF_FOLDER = /^[A-Za-z0-9][A-Za-z0-9 _-]{0,63}$/;
