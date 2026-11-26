import { describe, expect, it, vi } from 'vitest';
import { handleError, incidentOf } from './hooks.client.ts';
import { signInHref } from '#lib/session.ts';

describe('the failure of a screen (EF-UX-003)', () => {
  // req: EF-UX-003
  it('carries the request id the server gave', () => {
    expect(incidentOf({ code: 'boom', title: 'x', request_id: 'req-7' })).toBe('req-7');
  });

  // req: EF-UX-003
  it('invents no identifier when the server gave none', () => {
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
    expect(incidentOf(new Error('boom'))).toBeUndefined();
    const shown = handleError({ kind: 'unknown', error: new Error('secret detail'), event: {} as never });
    expect(shown).toEqual({ message: 'Une erreur est survenue.' });
    expect(handleError({ kind: 'framework', error: { status: 404, message: 'Not Found' }, event: {} as never })).toBeUndefined();
  });

  // req: EF-UX-003
  it('refuses an identifier that is not one', () => {
    expect(incidentOf({ request_id: '<img src=x>' })).toBeUndefined();
  });

  // req: EF-UX-003
  it('asks the sign-in page to come back, only to a path of the application', () => {
    expect(signInHref('/sante?x=1')).toBe('/login?next=%2Fsante%3Fx%3D1');
    expect(signInHref('//evil.example')).toBe('/login');
    expect(signInHref('https://evil.example')).toBe('/login');
  });
});
