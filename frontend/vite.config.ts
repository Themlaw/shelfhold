import adapter from '@sveltejs/adapter-static';
import { sveltekit } from '@sveltejs/kit/vite';
import { svelteTesting } from '@testing-library/svelte/vite';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vitest/config';
import { kitCsp } from './scripts/csp.ts';

const appHtml = fileURLToPath(new URL('./src/app.html', import.meta.url));

export default defineConfig({
  plugins: [
    sveltekit({
      adapter: adapter({ fallback: 'index.html' }),
      csp: { mode: 'hash', directives: kitCsp(readFileSync(appHtml, 'utf8')) }
    }),
    svelteTesting()
  ],
  test: {
    environment: 'jsdom',
    include: ['src/**/*.test.ts', 'scripts/**/*.test.ts'],
    setupFiles: ['./vitest.setup.ts'],
    coverage: { provider: 'v8', include: ['src/**/*.{ts,svelte}'], exclude: ['src/lib/api/**'] }
  }
});
