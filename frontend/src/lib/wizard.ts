// The first-run wizard (EF-UX-001): at most five screens in all; part 1 is the first three. The
// screens only show what the server answers: every route behind them is the admin's, on a fresh
// session (a stale one asks for the password in place, `ReauthPrompt`).
import type { MessageKey } from '#lib/i18n/index.ts';

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

/** The step after `id`, or null on the last one of this part. */
export function nextStep(id: WizardStep['id']): WizardStep | null {
  const at = WIZARD_STEPS.findIndex((s) => s.id === id);
  return WIZARD_STEPS[at + 1] ?? null;
}
