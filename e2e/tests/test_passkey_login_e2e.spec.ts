import { expect, test, type Page } from '@playwright/test';

// EF-USR-025, ADR-0050: a passkey registered then used to sign in, in a `localhost` context (the
// only plain-HTTP origin WebAuthn accepts). A virtual authenticator stands for the device; the
// SPA has a registration screen and no sign-in screen yet, so the sign-in is played with the same
// calls and the same conversions as `frontend/src/lib/passkeys.ts`.
const ORIGIN = 'http://localhost:8791';
const PASSWORD = 'synthetic-admin-password-1';

async function virtualAuthenticator(page: Page): Promise<void> {
  const cdp = await page.context().newCDPSession(page);
  await cdp.send('WebAuthn.enable');
  await cdp.send('WebAuthn.addVirtualAuthenticator', {
    options: {
      protocol: 'ctap2',
      transport: 'internal',
      hasResidentKey: true,
      hasUserVerification: true,
      isUserVerified: true,
      automaticPresenceSimulation: true
    }
  });
}

// req: EF-USR-025
test('test_passkey_login_e2e', async ({ page, browser }) => {
  await page.goto(`${ORIGIN}/`);
  await virtualAuthenticator(page);

  // password sign-in (the session that may add a passkey), then register one in the SPA
  const signedIn = await page.evaluate(async (password) => {
    const answer = await fetch('/api/v1/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: 'admin', password })
    });
    return answer.status;
  }, PASSWORD);
  expect(signedIn).toBe(200);
  await page.goto(`${ORIGIN}/compte/passkeys`);
  await page.getByRole('textbox').fill('Portable');
  await page.getByRole('button', { name: /ajouter/i }).click();
  await expect(page.getByText('Portable')).toBeVisible();

  // no session any more (cookies cleared): the passkey alone signs in
  await page.context().clearCookies();
  const anonymous = await page.evaluate(async () => (await fetch('/api/v1/auth/passkeys')).status);
  expect(anonymous).toBe(401);

  const result = await page.evaluate(async () => {
    const bytes = (text: string): Uint8Array<ArrayBuffer> => {
      const padded = text.replace(/-/g, '+').replace(/_/g, '/');
      const raw = atob(padded + '='.repeat((4 - (padded.length % 4)) % 4));
      return Uint8Array.from(raw, (char) => char.charCodeAt(0));
    };
    const text = (buffer: ArrayBuffer): string => {
      let raw = '';
      for (const byte of new Uint8Array(buffer)) raw += String.fromCharCode(byte);
      return btoa(raw).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
    };
    const json = { 'Content-Type': 'application/json' };
    const started = await (
      await fetch('/api/v1/auth/passkeys/login/options', { method: 'POST', headers: json })
    ).json();
    const credential = (await navigator.credentials.get({
      publicKey: {
        challenge: bytes(started.options.challenge),
        rpId: started.options.rpId,
        userVerification: 'required'
      }
    })) as PublicKeyCredential;
    const response = credential.response as AuthenticatorAssertionResponse;
    const answer = await fetch('/api/v1/auth/passkeys/login', {
      method: 'POST',
      headers: json,
      body: JSON.stringify({
        flow: started.flow,
        credential: {
          id: credential.id,
          rawId: text(credential.rawId),
          type: credential.type,
          response: {
            clientDataJSON: text(response.clientDataJSON),
            authenticatorData: text(response.authenticatorData),
            signature: text(response.signature),
            userHandle: response.userHandle ? text(response.userHandle) : null
          },
          clientExtensionResults: {}
        }
      })
    });
    return { rpId: started.options.rpId, status: answer.status };
  });
  expect(result.rpId).toBe('localhost');
  expect(result.status).toBe(200);
  const listed = await page.evaluate(async () => (await fetch('/api/v1/auth/passkeys')).status);
  expect(listed).toBe(200); // the passkey opened a session
});

// req: EF-USR-025
test('test_passkey_refused_from_a_foreign_origin', async ({ page }) => {
  // the same server reached by its 127.0.0.1 name is not the relying party: nothing to sign in with
  await page.goto('/');
  const status = await page.evaluate(async () => {
    const answer = await fetch('/api/v1/auth/passkeys/login/options', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' }
    });
    const body = await answer.json();
    return { status: answer.status, rpId: body?.options?.rpId ?? null };
  });
  expect(status.rpId).not.toBe('127.0.0.1'); // the RP ID comes from SHELFHOLD_PUBLIC_URL only
});
