// Shared by the "À classer" screen and the "À ranger" view (EF-USR-015, EF-EXP-046).
import type { MessageKey } from '#lib/i18n/index.ts';

/** The tiers the server accepts (`AGE_TIERS` plus X), as the text it parses. */
export const AGES = ['0+', '10+', '12+', '13+', '16+', '18+', 'X'] as const;
export type Age = (typeof AGES)[number];

const LABELS: Record<Age, MessageKey> = {
  '0+': 'age.0',
  '10+': 'age.10',
  '12+': 'age.12',
  '13+': 'age.13',
  '16+': 'age.16',
  '18+': 'age.18',
  X: 'age.x'
};
export const ageLabel = (age: Age): MessageKey => LABELS[age];

export type Problem = {
  code?: string;
  type?: string;
  status?: number;
  reauth_methods?: string[];
};

export function problemOf(error: unknown): Problem {
  return error && typeof error === 'object' ? (error as Problem) : {};
}

/** 401 `reauth-required` (EF-USR-022): the session is good, the password is too old. */
export function needsReauth(error: unknown): boolean {
  const problem = problemOf(error);
  return problem.code === 'reauth_required' || /reauth-required$/.test(problem.type ?? '');
}

/** 401 otherwise: no session at all. */
export function needsSession(error: unknown): boolean {
  return problemOf(error).status === 401 && !needsReauth(error);
}
