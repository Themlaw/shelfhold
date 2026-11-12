// What the error component shows (EF-UX-003): the stable code, the text, the cause and the action of
// an `application/problem+json` body, or, when the answer is not one the server wrote (a proxy page,
// a network failure, an unreadable body), a generic text and the request identifier to report.
// (Not in `api/`: that folder is generated and emptied by `open-api/generate.sh`.)
import { t } from '#lib/i18n/index.ts';

export interface ProblemView {
  /** The stable code of the catalog; null when the answer carried none we can trust. */
  code: string | null;
  title: string;
  cause: string | null;
  action: string | null;
  /** The identifier a person can report; always shown when there is one. */
  requestId: string | null;
}

const CODE = /^[a-z][a-z0-9_.]{0,63}$/;
const MAX_TEXT = 500;
const REQUEST_ID_HEADER = 'X-Request-ID';

function text(value: unknown): string | null {
  if (typeof value !== 'string') return null;
  const trimmed = value.trim();
  return trimmed ? trimmed.slice(0, MAX_TEXT) : null;
}

/** An answer with no usable code: the generic text, and the request id when we have one. */
export function genericProblem(requestId: string | null = null): ProblemView {
  return {
    code: null,
    title: t('problem.generic'),
    cause: null,
    action: null,
    requestId: text(requestId)
  };
}

/** The view of a parsed body; `headerRequestId` backs up a body that does not carry one. */
export function problemView(body: unknown, headerRequestId: string | null = null): ProblemView {
  const record = body !== null && typeof body === 'object' ? (body as Record<string, unknown>) : {};
  const requestId = text(record.request_id) ?? text(headerRequestId);
  const code = text(record.code);
  const title = text(record.title);
  if (code === null || !CODE.test(code) || title === null) return genericProblem(requestId);
  return { code, title, cause: text(record.cause), action: text(record.action), requestId };
}

/** The view of a failed response: reads the body only when it is a problem+json, never throws. */
export async function problemFromResponse(response: Response): Promise<ProblemView> {
  const requestId = response.headers.get(REQUEST_ID_HEADER);
  const type = response.headers.get('content-type') ?? '';
  if (!type.startsWith('application/problem+json')) return genericProblem(requestId);
  try {
    return problemView(await response.json(), requestId);
  } catch {
    return genericProblem(requestId);
  }
}
