// WCAG 2.2 contrast of the design tokens (ENF-A11Y-001): every text/background pair of every
// theme reaches AA, in light and in dark. `node scripts/contrast.ts` exits 1 on a failure.
import { readdirSync, readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

export const themesDir = join(dirname(fileURLToPath(import.meta.url)), '..', 'src', 'lib', 'themes');
export const AA_TEXT = 4.5;

type Group = { [key: string]: unknown };
export type Pair = { text: string; background: string };
export type Failure = { scheme: string; text: string; background: string; ratio: number };

// Text colors and the backgrounds they sit on. `on-accent` is the label of an accent button.
const TEXT_ON_PAGE = ['ink', 'muted', 'accent', 'ok', 'warn', 'err'];
const PAGE_BACKGROUNDS = ['bg', 'surface'];

function channel(value: number): number {
  const c = value / 255;
  return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
}

function rgb(hex: string): [number, number, number] {
  let h = hex.trim().replace(/^#/, '');
  if (h.length === 3) h = [...h].map((c) => c + c).join('');
  if (!/^[0-9a-fA-F]{6}$/.test(h)) throw new Error(`not a hex color: ${hex}`);
  return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16)) as [number, number, number];
}

export function luminance(hex: string): number {
  const [r, g, b] = rgb(hex);
  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
}

export function contrastRatio(a: string, b: string): number {
  const [la, lb] = [luminance(a), luminance(b)];
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
}

function colors(scheme: Group): Record<string, string> {
  const out: Record<string, string> = {};
  const group = (scheme.color ?? {}) as Group;
  for (const [name, node] of Object.entries(group)) {
    if (node && typeof node === 'object' && '$value' in node) out[name] = String(node.$value);
  }
  return out;
}

/** The pairs to check in one scheme, with their colors. */
export function pairsOf(scheme: Group): (Pair & { names: string })[] {
  const c = colors(scheme);
  const pairs: (Pair & { names: string })[] = [];
  const add = (text: string, background: string) => {
    if (c[text] && c[background]) {
      pairs.push({ text: c[text], background: c[background], names: `${text}/${background}` });
    }
  };
  for (const text of TEXT_ON_PAGE) for (const bg of PAGE_BACKGROUNDS) add(text, bg);
  add('on-accent', 'accent');
  return pairs;
}

export function failingPairs(tokens: { light: Group; dark?: Group }): (Failure & { names: string })[] {
  const failures: (Failure & { names: string })[] = [];
  for (const scheme of ['light', 'dark'] as const) {
    const group = tokens[scheme];
    if (!group) continue;
    for (const p of pairsOf(group)) {
      const ratio = contrastRatio(p.text, p.background);
      if (ratio < AA_TEXT) {
        failures.push({ scheme, text: p.text, background: p.background, names: p.names, ratio });
      }
    }
  }
  return failures;
}

export function checkAll(dir = themesDir): string[] {
  const lines: string[] = [];
  for (const e of readdirSync(dir, { withFileTypes: true }).filter((d) => d.isDirectory())) {
    const tokens = JSON.parse(readFileSync(join(dir, e.name, 'tokens.json'), 'utf8'));
    for (const f of failingPairs(tokens)) {
      lines.push(`${e.name} (${f.scheme}) ${f.names}: ${f.ratio.toFixed(2)} < ${AA_TEXT}`);
    }
  }
  return lines;
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const failures = checkAll();
  for (const line of failures) console.log(line);
  process.exit(failures.length ? 1 : 0);
}
