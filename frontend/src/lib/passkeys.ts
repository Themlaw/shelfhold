// Browser side of the passkey ceremonies (EF-USR-025). The server draws the challenge and
// verifies everything; this file only converts between the JSON the server sends (base64url
// fields) and the buffers `navigator.credentials` wants. No dependency.

export function toBytes(text: string): Uint8Array<ArrayBuffer> {
  const padded = text.replace(/-/g, '+').replace(/_/g, '/');
  const raw = atob(padded + '='.repeat((4 - (padded.length % 4)) % 4));
  return Uint8Array.from(raw, (char) => char.charCodeAt(0));
}

export function toText(buffer: ArrayBuffer): string {
  let raw = '';
  for (const byte of new Uint8Array(buffer)) raw += String.fromCharCode(byte);
  return btoa(raw).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

type Descriptor = { id: string; type: 'public-key'; transports?: AuthenticatorTransport[] };
type Json = Record<string, unknown>;

function descriptors(list: unknown): PublicKeyCredentialDescriptor[] | undefined {
  if (!Array.isArray(list)) return undefined;
  return (list as Descriptor[]).map((item) => ({ ...item, id: toBytes(item.id) }));
}

/** Where the passkey functions exist: a secure context with the browser API. */
export function passkeysSupported(): boolean {
  return (
    typeof window !== 'undefined' &&
    window.isSecureContext &&
    typeof PublicKeyCredential !== 'undefined' &&
    !!navigator.credentials
  );
}

export async function createPasskey(options: Json): Promise<Json> {
  const user = options.user as { id: string; name: string; displayName: string };
  const credential = (await navigator.credentials.create({
    publicKey: {
      ...(options as unknown as PublicKeyCredentialCreationOptions),
      challenge: toBytes(options.challenge as string),
      user: { ...user, id: toBytes(user.id) },
      excludeCredentials: descriptors(options.excludeCredentials)
    }
  })) as PublicKeyCredential | null;
  if (!credential) throw new Error('cancelled');
  const response = credential.response as AuthenticatorAttestationResponse;
  return {
    id: credential.id,
    rawId: toText(credential.rawId),
    type: credential.type,
    response: {
      clientDataJSON: toText(response.clientDataJSON),
      attestationObject: toText(response.attestationObject),
      transports: response.getTransports ? response.getTransports() : []
    },
    clientExtensionResults: {}
  };
}

export async function getPasskey(options: Json): Promise<Json> {
  const credential = (await navigator.credentials.get({
    publicKey: {
      challenge: toBytes(options.challenge as string),
      rpId: options.rpId as string,
      timeout: options.timeout as number | undefined,
      allowCredentials: descriptors(options.allowCredentials),
      userVerification: 'required'
    }
  })) as PublicKeyCredential | null;
  if (!credential) throw new Error('cancelled');
  const response = credential.response as AuthenticatorAssertionResponse;
  return {
    id: credential.id,
    rawId: toText(credential.rawId),
    type: credential.type,
    response: {
      clientDataJSON: toText(response.clientDataJSON),
      authenticatorData: toText(response.authenticatorData),
      signature: toText(response.signature),
      userHandle: response.userHandle ? toText(response.userHandle) : null
    },
    clientExtensionResults: {}
  };
}

/** The anti-forgery token the server set in a readable cookie (`__Host-csrf` over HTTPS). */
export function csrfToken(): string {
  for (const name of ['__Host-csrf', 'csrf']) {
    const match = document.cookie.split('; ').find((entry) => entry.startsWith(`${name}=`));
    if (match) return decodeURIComponent(match.slice(name.length + 1));
  }
  return '';
}
