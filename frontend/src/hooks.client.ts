// What an unexpected failure of a screen tells +error.svelte (EF-UX-003): a fixed sentence (never
// the message of the framework or of the server) and, only when the server wrote one, the
// identifier of the incident to report. An identifier invented here would point to nothing in the
// logs of the server, so none is made up: the screen words itself differently without one.
import type { HandleClientError } from '@sveltejs/kit/hooks';
import { t } from '#lib/i18n/index.ts';
import { problemView } from '#lib/problems.ts';

const MAX_ID = 128;
const SAFE_ID = /^[\w.:-]+$/;

/** The request id the server gave for a failure, from the problem it wrote or a failed response. */
export function incidentOf(error: unknown): string | undefined {
  if (error === null || typeof error !== 'object') return undefined;
  const record = error as Record<string, unknown>;
  const raw = problemView(record).requestId ?? (typeof record.requestId === 'string' ? record.requestId : null);
  return raw !== null && raw.length <= MAX_ID && SAFE_ID.test(raw) ? raw : undefined;
}

export const handleError: HandleClientError = (input) => {
  // the 404s of the router and the errors the app threw on purpose keep their own words
  if (input.kind !== 'unknown') return;
  const requestId = incidentOf(input.error);
  console.error('screen failure', requestId ?? '', input.error);
  return requestId ? { message: t('problem.generic'), requestId } : { message: t('problem.generic') };
};
