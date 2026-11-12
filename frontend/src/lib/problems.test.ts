import { describe, expect, it } from 'vitest';
import { genericProblem, problemFromResponse, problemView } from './problems';

const problem = {
  type: '/api/v1/problems/not-found',
  title: 'Ressource introuvable.',
  status: 404,
  code: 'not_found',
  action: 'Vérifiez l’adresse.',
  cause: 'L’élément n’existe pas.',
  request_id: 'req-1'
};
const response = (body: BodyInit, type: string, headers: Record<string, string> = {}) =>
  new Response(body, { status: 500, headers: { 'content-type': type, ...headers } });
const GENERIC = 'Une erreur est survenue.';

describe('problem view (EF-UX-003)', () => {
  // req: EF-UX-003
  it('keeps the code, the text, the cause, the action and the request id of a problem', () => {
    expect(problemView(problem)).toEqual({
      code: 'not_found',
      title: 'Ressource introuvable.',
      cause: 'L’élément n’existe pas.',
      action: 'Vérifiez l’adresse.',
      requestId: 'req-1'
    });
  });

  // req: EF-UX-003
  it('test_unknown_error_code_shows_request_id: no usable code gives the generic text and the id', () => {
    const bodies = [
      { title: 'x', request_id: 'req-9' },
      { code: 'Bad Code!', title: 'x', request_id: 'req-9' },
      { code: 'a_code', request_id: 'req-9' }
    ];
    for (const body of bodies) {
      expect(problemView(body)).toEqual({
        code: null,
        title: GENERIC,
        cause: null,
        action: null,
        requestId: 'req-9'
      });
    }
  });

  // req: EF-UX-003
  it('never throws and never shows what is not a text', () => {
    for (const body of [null, undefined, 7, 'oops', [], { code: 3, title: {}, cause: [] }]) {
      expect(problemView(body, 'req-h')).toMatchObject({ title: GENERIC, requestId: 'req-h' });
    }
    expect(problemView({ ...problem, cause: 42, action: '   ' })).toMatchObject({
      cause: null,
      action: null
    });
    expect(genericProblem().requestId).toBeNull();
  });

  // req: EF-UX-003
  it('bounds a text so that a hostile answer cannot flood the screen', () => {
    expect(problemView({ ...problem, cause: 'a'.repeat(5000) }).cause).toHaveLength(500);
  });

  // req: EF-UX-003
  it('reads a problem+json response; the header backs up a body without an id', async () => {
    const noId: Record<string, unknown> = { ...problem };
    delete noId.request_id;
    const view = await problemFromResponse(
      response(JSON.stringify(noId), 'application/problem+json', { 'X-Request-ID': 'req-h' })
    );
    expect(view).toMatchObject({ code: 'not_found', requestId: 'req-h' });
  });

  // req: EF-UX-003
  it('a response that is not a problem (proxy page, bad json) is generic with its request id', async () => {
    const html = await problemFromResponse(
      response('<html>502</html>', 'text/html', { 'X-Request-ID': 'req-p' })
    );
    expect(html).toMatchObject({ code: null, title: GENERIC, requestId: 'req-p' });
    const broken = await problemFromResponse(response('{not json', 'application/problem+json'));
    expect(broken).toMatchObject({ code: null, title: GENERIC });
  });
});
