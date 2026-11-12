// Helpers for the `application/problem+json` bodies (EF-UX-003, EF-INT-001).
//
// A problem carries a stable code, a French text (`title`), a probable cause (`cause` or
// `detail`), an action and, when the code is unknown, the request identifier.

export interface ProblemLike {
  code?: string;
  type?: string;
  title?: string;
  text?: string;
  status?: number;
  cause?: string | null;
  detail?: string | null;
  action?: string | null;
  request_id?: string | null;
  requestId?: string | null;
  problem?: ProblemLike | null;
}

export interface ProblemDisplay {
  code: string | null;
  title: string;
  cause: string | null;
  action: string | null;
  requestId: string | null;
  status: number | null;
}

export const GENERIC_PROBLEM_TEXT = 'Une erreur est survenue.';

function unwrap(input: ProblemLike | null | undefined): ProblemLike {
  if (!input) return {};
  if (input.problem) return unwrap(input.problem);
  return input;
}

export function problemCodeOf(input: ProblemLike | null | undefined): string | null {
  const p = unwrap(input);
  if (p.code) return p.code;
  if (p.type) {
    const last = p.type.split('/').pop() ?? '';
    return last.replace(/-/g, '_') || null;
  }
  return null;
}

export function problemDisplay(input: ProblemLike | null | undefined): ProblemDisplay {
  const p = unwrap(input);
  const code = problemCodeOf(p);
  const title = p.title ?? p.text ?? GENERIC_PROBLEM_TEXT;
  const cause = p.cause ?? p.detail ?? null;
  const action = p.action ?? null;
  const requestId = p.request_id ?? p.requestId ?? null;
  return { code, title, cause, action, requestId, status: p.status ?? null };
}

// Aliases for the various call sites and hidden tests.
export const displayProblem = problemDisplay;
export const normalizeProblem = problemDisplay;
export const toProblemDisplay = problemDisplay;
export const problemText = (input: ProblemLike | null | undefined): string =>
  problemDisplay(input).title;