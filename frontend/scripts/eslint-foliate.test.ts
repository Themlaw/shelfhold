// "foliate is only imported by its adapter" (ADR-0055, ENF-SEC-004 volet lecteur): the rule of the
// ESLint config, played on the code it protects, with the paths the adapter really uses.
import { ESLint } from 'eslint';
import { describe, expect, it, vi } from 'vitest';

vi.setConfig({ testTimeout: 120_000 }); // the first lint loads the whole configuration

const eslint = new ESLint({ cwd: process.cwd() });
const messages = async (code: string, filePath: string) => {
  const [result] = await eslint.lintText(code, { filePath });
  return result!.messages.filter((m) => m.ruleId === 'no-restricted-imports' || m.ruleId === 'no-restricted-syntax');
};

describe('foliate is only imported by its adapter', () => {
  const imports = [
    "import { View } from '../../../../vendor/foliate-js/view.js';",
    "import { EPUB } from '../../../vendor/foliate-js/epub.js';",
    "import '../../vendor/foliate-js/view.js';",
    "const m = await import('../vendor/foliate-js/pdf.js');",
    "import { x } from 'foliate-js/view.js';",
    "export * from '../vendor/foliate-js/footnotes.js';"
  ];

  for (const line of imports) {
    // req: ENF-SEC-004
    it(`refuses ${line} anywhere else`, async () => {
      for (const file of ['src/lib/reader/engines/images.ts', 'src/lib/reader/filter.ts', 'src/lib/reader/registry.ts', 'src/routes/page.ts']) {
        expect((await messages(line, file)).length, `${file}: ${line}`).toBeGreaterThan(0);
      }
    });
  }

  // req: ENF-SEC-004
  it('lets the adapter, and only the adapter, import it', async () => {
    expect(await messages(imports.join('\n'), 'src/lib/reader/engines/foliate.ts')).toEqual([]);
    expect((await messages(imports[0]!, 'src/lib/reader/engines/foliate.test.ts')).length).toBeGreaterThan(0);
  });
});
