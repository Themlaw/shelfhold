# Spike S2 — front SvelteKit (ADR-0013)

Code jetable (`spikes/s2/`), mesuré le 2026-11-04 sur Chromium headless 156 (Playwright 1.64), Linux, Node 24.19.
Reproduire : `npm ci && ../../open-api/generate.sh` (schéma) puis `npx openapi-ts && npx vite build && npx playwright test`.

## Versions (dernières stables publiées il y a au moins 14 jours, soit au plus le 2026-10-21)

| Paquet | Version retenue | Plus récente (trop récente) |
| --- | --- | --- |
| `@sveltejs/kit` | 3.1.0 (2026-10-14) | 3.1.1 (2026-10-29) |
| `svelte` | 5.57.2 (2026-10-06) | 5.57.3 (2026-10-26) |
| `@sveltejs/adapter-static` | 4.0.0 | 4.0.1 (2026-10-29) |
| `@sveltejs/vite-plugin-svelte` / `vite` / `typescript` | 7.3.1 / 8.3.4 / 6.0.3 | |

SvelteKit 3 existe en stable (3.0.0 le 2026-10-01) : aucune substitution de majeure. Les paquets
sont installés avec `npm --before=2026-10-21` (la règle des 14 jours de ADR-0016, côté npm).

## Les six critères

| # | Critère | Résultat | Mesure |
| --- | --- | --- | --- |
| 1 | Build statique servi par FastAPI | Passe | `vite build` en 1,0 s (8,6 s avec npm) ; `GET /` et `GET /bibliotheque` (repli SPA) répondent 200 depuis `create_http_app(frontend_dir=…)` |
| 2 | Client OpenAPI généré branché | Passe | `@hey-api/openapi-ts` 0.99 sur le schéma exporté sans serveur ; la page appelle `healthz()` et affiche « ok » |
| 3 | Liste virtualisée de 2 000 éléments | Passe | `@tanstack/svelte-virtual` 3.13 ; 27 nœuds DOM au plus ; balayage complet en 240 images : p50 16,7 ms, p95 18,1 ms, max 22,9 ms, 0 tâche longue |
| 4 | i18n FR, pluriels ICU | Passe | `intl-messageformat` 12 : « Aucune œuvre / 1 œuvre / 2 000 œuvres » (0 et 1 au singulier, séparateur de milliers français) |
| 5 | axe sans violation serious/critical, 3 écrans | Passe | Accueil, Bibliothèque, Réglages : 0 violation (la première passe a relevé `document-title` manquant, corrigé) |
| 6 | Taille du bundle initial | Mesuré | Page d'accueil : 12 fichiers, 133,6 ko bruts, 49,0 ko gzip (dont `intl-messageformat`, environ 35 ko bruts) |

Mesure de fluidité en headless sans GPU : indicative, à refaire sur appareil modeste en M3. Le
spike n'a pas évalué le lecteur foliate, la PWA ni le TTI (hors des six critères de la feuille de route).

## Confort de développement : points d'attention de SvelteKit 3

- `svelte.config.js` n'est plus lu : la configuration (adapter, CSP) passe par `sveltekit({...})` dans `vite.config.ts`.
- L'alias `$lib` est remplacé par les imports de sous-chemin : `"imports": {"#lib/*": "./src/lib/*"}` dans `package.json`, avec **extension explicite** (`#lib/i18n.ts`).
- `tsconfig.json` doit étendre `$app/tsconfig` (généré par `svelte-kit sync`, qui doit précéder tout outil lisant le tsconfig, dont l'outil de génération du client).
- `openapi-typescript` 7 déclare `typescript@^5` en pair et refuse TypeScript 6 : le client est généré par `@hey-api/openapi-ts`.
- `msw` 3.0.3 échoue à l'import sous Vitest 5 / Node 24 (`./ClientRequest` absent des exports d'`@mswjs/interceptors` 0.45.9) : `msw` 2.15 est utilisé.
- Avec la CSP à hash, SvelteKit pose un attribut `style` (annonceur de route) : il faut `style-src-attr` avec `unsafe-hashes` et son empreinte, sinon violation CSP (détectée par le test e2e).

## Conclusion

**SvelteKit 3 est retenu** (React 19 non déclenché). Les six critères passent, aucun manque bloquant
de composants (la virtualisation, l'i18n ICU et le client généré sont couverts par des paquets
indépendants du framework). Le client généré et la CSP à hash sont opérationnels dès M0.
Réserves suivies : le lecteur foliate et le TTI sur matériel modeste seront évalués en M3/M5 ; le
mécanisme des imports `#lib` et l'écosystème de composants (shadcn-svelte, bits-ui) restent à confirmer à l'usage.
