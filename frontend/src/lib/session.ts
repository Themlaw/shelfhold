// Signing out (EF-USR-022): the offline copies of the profile are wiped first (signing out
// offline, or a request that fails, still leaves nothing of it on the device, L16b), then the
// session is closed on the server, then the browser loads the sign-in page of the server. That
// page is not a screen of this application: a full load, never the router.
import { logout } from '#lib/api/index.ts';
import { wipeOfflineCopies } from '#lib/offline/runtime.ts';
import { csrfToken } from '#lib/passkeys.ts';

export const SIGN_IN_PAGE = '/login';

/** The sign-in page, asking to come back to `path` (a path of this application) once signed in. */
export function signInHref(path: string | null | undefined): string {
  if (!path || !path.startsWith('/') || path.startsWith('//') || path.startsWith('/login')) return SIGN_IN_PAGE;
  return `${SIGN_IN_PAGE}?next=${encodeURIComponent(path)}`;
}

/** Close the session; `false` when the server could not be told (the session may still be open). */
export async function signOut(navigate: (url: string) => void = (url) => location.assign(url)): Promise<boolean> {
  await wipeOfflineCopies();
  try {
    await logout({ headers: { 'X-CSRF-Token': csrfToken() }, throwOnError: true });
  } catch {
    return false;
  }
  navigate(SIGN_IN_PAGE);
  return true;
}
