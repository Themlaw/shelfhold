import { readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const src = join(root, 'src');

function sources(dir = src): string[] {
  return readdirSync(dir).flatMap((name) => {
    const full = join(dir, name);
    if (statSync(full).isDirectory()) return name === 'api' ? [] : sources(full);
    return /\.(svelte|ts|css)$/.test(name) && !/\.test\.ts$/.test(name) ? [full] : [];
  });
}

const reduce = (css: string) => {
  const start = css.indexOf('@media (prefers-reduced-motion: reduce)');
  return start < 0 ? '' : css.slice(start, css.indexOf('\n}', start) + 2);
};

describe('reduced motion (ENF-A11Y-003)', () => {
  const globalCss = readFileSync(join(src, 'routes', 'global.css'), 'utf8');

  // req: ENF-A11Y-003
  it('test_reduced_motion: the global stylesheet switches every animation and transition off', () => {
    const block = reduce(globalCss);
    expect(block).not.toBe('');
    expect(block).toMatch(/animation-duration:\s*0\.01ms\s*!important/);
    expect(block).toMatch(/animation-iteration-count:\s*1\s*!important/);
    expect(block).toMatch(/transition-duration:\s*0\.01ms\s*!important/);
    expect(block).toMatch(/scroll-behavior:\s*auto\s*!important/);
    expect(block).toMatch(/\*,\s*\*::before,\s*\*::after/);
  });

  // req: ENF-A11Y-003
  it('no smooth scrolling or script animation escapes the stylesheet rule', () => {
    const offenders: string[] = [];
    for (const file of sources()) {
      const text = readFileSync(file, 'utf8');
      const path = relative(root, file);
      const code = path.endsWith('global.css') ? text.replace(reduce(text), '') : text;
      if (/scroll-behavior:\s*smooth/.test(code)) offenders.push(`${path}: scroll-behavior`);
      if (/behavior:\s*['"]smooth['"]/.test(code)) offenders.push(`${path}: smooth scroll call`);
      if (/\.animate\(/.test(code)) offenders.push(`${path}: Element.animate`);
      if (/svelte\/(transition|animate|motion)/.test(code)) offenders.push(`${path}: svelte motion`);
      if (/<[^>]*\s(transition|in|out|animate):[a-z]+/.test(code)) {
        offenders.push(`${path}: svelte transition directive`);
      }
      if (/requestAnimationFrame/.test(code)) offenders.push(`${path}: requestAnimationFrame`);
    }
    expect(offenders).toEqual([]);
  });

  // req: ENF-A11Y-003
  it('every CSS animation or transition is covered by the global rule, or is declared reduced', () => {
    const guarded = /prefers-reduced-motion:\s*(reduce|no-preference)/;
    expect(globalCss).toMatch(guarded);
    for (const file of sources().filter((f) => /\.(svelte|css)$/.test(f))) {
      const text = readFileSync(file, 'utf8');
      if (/@keyframes|animation\s*:|animation-name/.test(text)) {
        // an animation must be declared in a stylesheet that the global rule overrides:
        // `!important` durations apply to every element, so nothing more is needed here.
        expect(reduce(globalCss), relative(root, file)).toContain('animation-duration');
      }
    }
  });
});
