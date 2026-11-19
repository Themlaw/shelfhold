// What the address of the reader says it should open.

/** `/lire/unit/U1?scope=excerpt` → what the reader opens; anything else is nothing to open. */
export function parseLocation(pathname: string, search: string): { carrierType: string; carrierId: string; scope: 'excerpt' | null } | null {
  const match = /^\/lire\/([A-Za-z_]{1,32})\/([A-Za-z0-9_-]{1,64})\/?$/.exec(pathname);
  if (!match) return null;
  return {
    carrierType: match[1]!,
    carrierId: match[2]!,
    scope: new URLSearchParams(search).get('scope') === 'excerpt' ? 'excerpt' : null
  };
}
