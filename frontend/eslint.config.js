import js from '@eslint/js';
import svelte from 'eslint-plugin-svelte';
import globals from 'globals';
import ts from 'typescript-eslint';

export default ts.config(
  { ignores: ['build/', '.svelte-kit/', 'node_modules/', 'coverage/', 'src/lib/api/', 'src/lib/themes/themes.css'] },
  js.configs.recommended,
  ...ts.configs.recommended,
  ...svelte.configs.recommended,
  { languageOptions: { globals: { ...globals.browser, ...globals.node } } },
  {
    files: ['**/*.svelte', '**/*.svelte.ts'],
    languageOptions: { parserOptions: { parser: ts.parser } }
  },
  // I51 (ADR-0051): no component, screen or store reads the name of the theme. They read CSS
  // custom properties (colors, variants); only `src/lib/themes/` and the root layout know names.
  {
    files: ['src/**/*.{ts,svelte}'],
    rules: {
      'no-restricted-syntax': [
        'error',
        ...[
          [
            "MemberExpression[object.type='MemberExpression'][object.property.name='dataset'][property.name=/^(theme|colorScheme)$/]",
            'Do not read the theme name or the color scheme from `dataset`'
          ],
          ['Literal[value=/data-(theme|color-scheme)/]', 'Do not address the data-theme attribute'],
          ['TemplateElement[value.raw=/data-(theme|color-scheme)/]', 'Do not address the data-theme attribute'],
          ['Literal[value=/^(bibliotheque|console|galerie|calme)$/]', 'Do not name a theme'],
          [
            "SvelteAttribute[key.name=/^data-(theme|color-scheme)$/]",
            'Do not set the theme attribute from markup'
          ],
          [
            "ImportDeclaration[source.value=/themes\\/index/]",
            'Only the root layout reads the theme mechanism (I51): use CSS custom properties'
          ]
        ].map(([selector, message]) => ({ selector, message: `theme: ${message} (I51)` }))
      ]
    }
  },
  {
    files: ['src/lib/themes/**', 'src/routes/+layout.ts', 'src/routes/+layout.svelte', 'src/**/*.test.ts'],
    rules: { 'no-restricted-syntax': 'off' }
  }
);
