import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { compileServerPages, TARGET } from './build-server-pages.ts';

describe('the stylesheet of the pages the backend writes', () => {
  // req: EF-UX-012
  it('the copy the backend serves is the one the theme and the foundation produce', () => {
    // npm run pages:css rewrites it
    expect(readFileSync(TARGET, 'utf8')).toBe(compileServerPages());
  });

  // req: EF-UX-012
  it('carries the fonts of the instance, never a remote one, and both colour schemes', () => {
    const css = compileServerPages();
    expect(css).toContain("url('/fonts/alegreya-latin-wght-normal.woff2')");
    expect(css).not.toMatch(/https?:|\/\/fonts\./);
    expect(css).toContain('--color-ink: #1D1B18');
    expect(css).toContain("[data-color-scheme='dark']");
    expect(css).toContain('prefers-color-scheme: dark');
  });
});
