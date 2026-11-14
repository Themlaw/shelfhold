---
status: "accepté (spike S2 conclu le 2026-11-04 : SvelteKit retenu)"
date: 2026-11-02
amended: 2026-11-02 (passe 1) ; 2026-11-02 (passe 2) ; 2026-11-04 (porteur 2) ; 2026-11-04 (spike S2)
decision-makers: mainteneur
---
# ADR-0013 — Front : SvelteKit (adapter-static, SPA) + vite-pwa

> **Amendée le 2026-11-02 (passes 1 et 2).** En cas de divergence, la section « Amendement passe 1 » prévaut sur le texte d'origine, conservé pour l'historique.

## Contexte et problème
Le front sert la bibliothèque, la recherche, la file d'activité, l'admin (groupes, cibles, sources) et le lecteur. Celui-ci embarque des composants impératifs (le custom element `<foliate-view>`, pdf.js). Il doit fonctionner hors ligne (PWA, V2), sur des téléphones modestes et des navigateurs e-ink, et être maintenu par une seule personne. Quatre directions de design sont prévues (A Bibliothèque par défaut, B Console dense, C Gallery, D Calm/e-ink), avec des tokens thémables (D24).

## Facteurs de décision
- Intégration de web components impératifs.
- Poids du bundle et performance sur matériel modeste.
- Écosystème de composants (listes virtualisées, tables).
- Productivité solo ; support PWA ; sortie statique servie par le backend.

## Options considérées
### A. SvelteKit `adapter-static` (`fallback: 'index.html'`) + `@vite-pwa/sveltekit`
- Avantages : peu de code de liaison (`bind:this` + action) pour foliate ; pas de DOM virtuel ; routes du lecteur chargées à la demande ; stores et transitions intégrés ; sortie statique.
- Inconvénients : écosystème plus petit que React ; Biome ne couvre pas entièrement Svelte (ESLint/Prettier nécessaires).
### B. React + Vite
- Avantages : plus grand écosystème (shadcn/ui, TanStack Virtual) ; utilisé par Stump et Suwayomi-WebUI (dont le lecteur MPL sert de référence).
- Inconvénients : boilerplate refs/effects pour les éléments impératifs ; plus lourd sur e-ink.
### C. Vue/Nuxt
- Avantages : solide ; Komga et Audiobookshelf l'utilisent (le lecteur Komga, MIT, sert de référence).
- Inconvénients : pas d'avantage décisif sur A pour ce cas.
### D. HTMX (rendu serveur)
- Avantages : très simple pour l'admin.
- Inconvénients : mauvais pour un lecteur à état client riche et pour le hors ligne.

## Décision
*Texte d'origine (proposition ; l'amendement ci-dessous l'accepte avec SvelteKit 3).* (D23) : option A, validée par un spike en M0. Le spike consiste en un écran bibliothèque avec liste virtualisée de 5 000 Œuvres et un lecteur foliate avec CSP stricte ; on mesure bundle, TTI et confort de développement. Si le spike révèle un manque bloquant de composants, on bascule vers B. Dans les deux cas : client API généré depuis l'OpenAPI, i18n FR/EN dès le premier commit, tokens de design thémables.

## Conséquences
- Positives : bundle léger, intégration simple du lecteur, PWA facile.
- Négatives : moins de composants prêts à l'emploi ; recrutement de contributeurs un peu plus étroit.

## Risque principal
Manque de composants mûrs (tables denses, virtualisation) qui coûte du temps au mainteneur. Mitigation : critères du spike explicites, et choix voté avant M2 (voir `06-points-discutables.md`).

## Réversibilité
Moyenne avant M2 (peu d'écrans), faible après M3 (lecteur et écrans à réécrire). Le backend n'est pas concerné (SPA statique + client généré).

## Amendement passe 1 (2026-11-02)
- **Décision** (A14) : SvelteKit **3** en SPA `adapter-static`, versions épinglées, **validé par le spike S2 en M0** (6 critères : liste virtualisée de 5 000 Œuvres, bundle, TTI, CSP à base de hash, PWA, confort de développement). **Repli documenté : React 19** + Vite + TanStack Router. L'option D (HTMX) est rejetée pour le produit (SIM-12, rejet partiel) mais le rendu serveur est retenu pour la page **`/lite`** e-ink (M5).
- **Versions vérifiées** (09-closure-B) : `@sveltejs/kit` 3.0.0 publié le 2026-10-01 (3.1.1 courante), `svelte` 5.57.3, `@sveltejs/adapter-static` 4.0.1, `@vite-pwa/sveltekit` 2.0.0 (2026-11-01), `shadcn-svelte` 1.7.2, `bits-ui` 2.20.3 ; repli `react` 19.3.1, `@tanstack/react-router`.
- **Périmètre UI V1 = atelier** (A1, A14) : assistant de premier lancement, recherche, étagères, Activité, demandes famille, réglages. Le lecteur et la PWA hors ligne passent en M5.
- **CSP de la SPA à base de hash** (pas de nonce), aucune ressource tierce dans le navigateur (A26, ENF-PRIV-001).
- **i18n** : français dès M0, anglais avant 1.0 (ENF-I18N-001) ; lint de vocabulaire UI (ENF-I18N-004, A30).
- **Une seule direction de design** pour la 1.0, avec densité admin (Q18).

## Amendement passe 2 (2026-11-02)
- **Liseuses e-ink** (B24, `research/10-closure-C.md` §8) : la version exacte de Chromium des Kindle ≥ 5.16.4 n'est publiée nulle part ; le navigateur Kobo est un WebKit ancien ; le navigateur intégré Boox n'est pas documenté. Décision robuste : **aucune détection par User-Agent** ; la SPA cible ES2020 et un test de capacités au chargement (`<script type="module">` + repli `nomodule`) redirige vers `/lite` ; `/lite` reste le chemin garanti ; « SPA au mieux » pour Kindle ≥ 5.16.4 et Boox (Chrome conseillé) ; une vérification sur appareil réel par famille en M3/M5.
- **Discrétion** (ADR-0030) : aucune balise Open Graph ni Twitter Card dans la SPA ; page de connexion neutre.
- foliate-js et SvelteKit sont inscrits dans `docs/dependencies.md` (ENF-GOV-002).

## Amendement porteur 2 (2026-11-04)
- **Quatre thèmes, A par défaut** (`review/ARBITRAGES-porteur2.md`, Q2 ; ADR-0051, EF-UX-012) : les directions A « Bibliothèque », B « Console », C « Galerie », D « Calme » deviennent des thèmes choisis par chaque utilisateur, sur **un seul socle de composants et d'écrans**. La règle de la passe 1 « une seule direction de design pour la 1.0 » (Q18) est conservée pour la livraison : la 1.0 (M3) livre le mécanisme de thèmes et A ; B, C, D et le sélecteur arrivent en M4. `src/lib/tokens.css` devient `src/lib/themes/<thème>/tokens.json` compilé en propriétés CSS.

## Amendement spike S2 (2026-11-04)
- **Décision confirmée : SvelteKit 3 est retenu**, le repli React 19 n'est pas déclenché. Mesures détaillées dans `spikes/s2/RESULTATS.md` : build statique servi par FastAPI, client généré branché, liste virtualisée de 2 000 éléments (p95 18,1 ms par image, aucune tâche longue), pluriels ICU français, axe sans violation serious/critical sur 3 écrans, bundle initial de l'accueil à 49 ko gzip.
- **Versions réellement épinglées** (dernières stables d'au moins 14 jours au 2026-11-04) : `@sveltejs/kit` 3.1.0 (la 3.1.1 date du 2026-10-29), `svelte` 5.57.2, `@sveltejs/adapter-static` 4.0.0, `vite` 8.3.4, `typescript` 6.0.3. Les versions citées en passe 1 (3.1.1, 5.57.3, 4.0.1) sont les plus récentes mais trop jeunes pour la règle des 14 jours.
- **Écarts SvelteKit 3** : configuration dans `vite.config.ts` (plus de `svelte.config.js`), imports `#lib/*` avec extension au lieu de `$lib`, `style-src-attr` avec empreinte dans la CSP à hash. Client OpenAPI : `@hey-api/openapi-ts` (`openapi-typescript` incompatible avec TypeScript 6). Tests : `msw` 2.15 (la 3.0.3 échoue sous Vitest 5).
- **Linter** : ESLint (plugin Svelte) et `svelte-check`, Biome ne couvrant pas Svelte entièrement.
- Reste à évaluer en M3/M5 : le lecteur foliate, le TTI sur matériel modeste et `@vite-pwa/sveltekit`.

## Exigences liées
EF-UX-001, EF-UX-005, ENF-A11Y-001, ENF-A11Y-004, ENF-I18N-001, ENF-I18N-004, ENF-PERF-004, ENF-PERF-010, ENF-SEC-004, ENF-COMP-002.

## Sources
- https://github.com/johnfactotum/foliate-js
- https://github.com/Suwayomi/Suwayomi-WebUI
- https://github.com/stumpapp/stump
- https://github.com/gotson/komga
- https://github.com/advplyr/audiobookshelf
- https://blakecrosley.com/guides/design/readwise-reader
- https://www.npmjs.com/package/@sveltejs/kit
- https://www.npmjs.com/package/@sveltejs/adapter-static
- https://www.npmjs.com/package/@vite-pwa/sveltekit
- https://www.npmjs.com/package/shadcn-svelte
- https://www.npmjs.com/package/bits-ui
- https://www.npmjs.com/package/react
- https://www.npmjs.com/package/@tanstack/react-router
