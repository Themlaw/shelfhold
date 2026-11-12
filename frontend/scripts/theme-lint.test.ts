import { readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import { ESLint } from 'eslint';
import { describe, expect, it } from 'vitest';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const eslint = new ESLint({ cwd: root });

async function themeMessages(code: string, file: string): Promise<string[]> {
  const [result] = await eslint.lintText(code, { filePath: join(root, file) });
  return result.messages
    .filter((m) => m.ruleId === 'no-restricted-syntax' && /theme/i.test(m.message))
    .map((m) => m.message);
}

const COMPONENT = 'src/lib/components/Probe.svelte';
const wrap = (script: string, markup = '<p>x</p>') =>
  `<script lang="ts">\n${script}\n</script>\n${markup}\n`;

describe('no component reads the name of the theme (I51)', () => {
  // req: EF-UX-012
  it.each([
    ['dataset', wrap("const n = document.documentElement.dataset.theme;\nconsole.log(n);")],
    ['attribute', wrap("const n = document.documentElement.getAttribute('data-theme');\nconsole.log(n);")],
    ['selector', wrap("const n = document.querySelector('[data-theme]');\nconsole.log(n);")],
    ['literal name', wrap("const mine = 'bibliotheque';\nconsole.log(mine);")],
    ['import of the list', wrap("import { THEMES } from '#lib/themes/index.ts';\nconsole.log(THEMES);")],
    ['markup attribute', wrap('', '<div data-theme="bibliotheque">x</div>')],
    ['color scheme attribute', wrap("document.documentElement.setAttribute('data-color-scheme', 'dark');")]
  ])('refuses a component that reads or writes it: %s', async (_name, code) => {
    expect(await themeMessages(code, COMPONENT)).not.toEqual([]);
  });

  // req: EF-UX-012
  it('refuses it in a store or a helper of the lib too', async () => {
    const code = "export const isDark = () => document.documentElement.dataset.colorScheme === 'dark';\n";
    expect(await themeMessages(code, 'src/lib/store.ts')).not.toEqual([]);
    expect(await themeMessages(code, 'src/routes/a-classer/+page.svelte')).not.toEqual([]);
  });

  // req: EF-UX-012
  it('accepts a component that only reads CSS properties', async () => {
    const code = wrap("let { label } = $props();", '<p style:color="var(--color-ink)">{label}</p>');
    expect(await themeMessages(code, COMPONENT)).toEqual([]);
  });

  // req: EF-UX-012
  it('lets the theme mechanism and the root layout do their job', async () => {
    const code = "document.documentElement.dataset.theme = 'bibliotheque';\n";
    expect(await themeMessages(code, 'src/lib/themes/index.ts')).toEqual([]);
    expect(await themeMessages(code, 'src/routes/+layout.ts')).toEqual([]);
  });

  // req: EF-UX-012
  it('test_no_component_reads_theme_name', () => {
    const offenders: string[] = [];
    const allowed = (path: string) =>
      path.startsWith(join('src', 'lib', 'themes')) ||
      /^src\/routes\/\+layout\.(ts|svelte)$/.test(path.split('\\').join('/')) ||
      /\.test\.ts$/.test(path);
    const walk = (dir: string) => {
      for (const name of readdirSync(dir)) {
        const full = join(dir, name);
        if (statSync(full).isDirectory()) {
          if (name !== 'api') walk(full);
          continue;
        }
        const path = relative(root, full);
        if (allowed(path) || !/\.(svelte|ts|css)$/.test(name)) continue;
        const text = readFileSync(full, 'utf8');
        if (/data-theme|data-color-scheme|dataset\.(theme|colorScheme)|bibliotheque|\[data-theme/.test(text)) {
          offenders.push(path);
        }
      }
    };
    walk(join(root, 'src'));
    expect(offenders).toEqual([]);
  });

  // req: EF-UX-012
  it('the source tree passes the rule', async () => {
    const results = await eslint.lintFiles(['src/**/*.{ts,svelte}']);
    const found = results.flatMap((r) =>
      r.messages
        .filter((m) => m.ruleId === 'no-restricted-syntax' && /theme/i.test(m.message))
        .map((m) => `${relative(root, r.filePath)}: ${m.message}`)
    );
    expect(found).toEqual([]);
  });
});
