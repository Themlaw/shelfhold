// The CSP of the SPA (ENF-SEC-004): by hash, never `unsafe-inline`, never a random nonce (the SPA
// is static, so the hash is fixed when it is built). The one inline script is the boot script of
// `src/app.html`, which sets the theme attributes before the first render; its hash is computed
// here from the very bytes of the page, so the CSP cannot drift from the script that is served.
import { createHash } from 'node:crypto';

/** The text of the single inline script of `app.html`. */
export function bootScript(html: string): string {
  const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)];
  const [only] = scripts;
  if (scripts.length !== 1 || !only) {
    throw new Error(`app.html must hold exactly one inline script, not ${scripts.length}`);
  }
  return only[1] ?? '';
}

/** A CSP source expression for `text`: `'sha256-<base64>'`. */
export function scriptHash(text: string): string {
  return `'sha256-${createHash('sha256').update(text).digest('base64')}'`;
}

// SvelteKit's route announcer carries a style attribute; allow only that hash.
const ANNOUNCER_STYLE = "'sha256-S8qMpvofolR8Mpjy4kQvEm7m1q8clzU4dfDH0AmvZjo='";

/** The `csp.directives` of SvelteKit (hash mode adds the hashes of its own inline script). */
export function kitCsp(html: string): Record<string, string[]> {
  return {
    'default-src': ["'self'"],
    'script-src': ["'self'", scriptHash(bootScript(html))],
    'style-src': ["'self'"],
    'base-uri': ["'none'"],
    'form-action': ["'self'"],
    'object-src': ["'none'"],
    'style-src-attr': ["'unsafe-hashes'", ANNOUNCER_STYLE]
  };
}
