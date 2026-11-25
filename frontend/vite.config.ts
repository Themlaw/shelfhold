import adapter from '@sveltejs/adapter-static';
import { withPwa } from '@vite-pwa/sveltekit';
import { svelteTesting } from '@testing-library/svelte/vite';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vitest/config';
import { kitCsp } from './scripts/csp.ts';

const appHtml = fileURLToPath(new URL('./src/app.html', import.meta.url));

export default defineConfig({
  plugins: [
    // The installable shell (L16a, D14): the worker is `src/service-worker.ts`, built here with the
    // list of the files of the build. Registered by the SPA itself (`injectRegister: false`: the
    // plugin's own would add an inline script to a page whose policy is by hash), and the manifest
    // is the static file of `static/` (`manifest: false`). Only the shell and the hashed assets are
    // precached: no `prerendered/` page exists (SPA), and the reader is not part of this build.
    withPwa(
      {
        adapter: adapter({ fallback: 'index.html' }),
        csp: { mode: 'hash', directives: kitCsp(readFileSync(appHtml, 'utf8')) },
        // SvelteKit's own registration is an inline script (and a module worker): the SPA registers
        // the worker itself, from a module of the origin (`src/lib/pwa/register.ts`)
        serviceWorker: { register: false }
      },
      {
        strategies: 'buildSW',
        filename: 'service-worker.js',
        manifest: false,
        injectRegister: false,
        buildSW: {
          swSrc: 'src/service-worker.ts',
          globPatterns: [
            'client/_app/immutable/**/*.{js,css}',
            'client/*.webmanifest',
            'client/icons/*.png',
            'client/fonts/*.woff2' // the fonts of the interface: the shell reads the same offline
          ],
          globStrict: false // the plugin adds `prerendered/**`, which a SPA has none of
        },
        kit: { adapterFallback: 'index.html', spa: true }
      }
    ),
    svelteTesting()
  ],
  test: {
    environment: 'jsdom',
    include: ['src/**/*.test.ts', 'scripts/**/*.test.ts'],
    setupFiles: ['./vitest.setup.ts'],
    coverage: { provider: 'v8', include: ['src/**/*.{ts,svelte}'], exclude: ['src/lib/api/**'] }
  }
});
