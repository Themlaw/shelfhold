# Benchmarks

Source : [ENF-PERF-006](../spec/01-cahier-des-charges.md) (page suivante < 150 ms depuis le cache), [§7.0 Profil de volumétrie de référence](../spec/01-cahier-des-charges.md) (ENF-PERF-008), [03-roadmap-tdd §4](../spec/03-roadmap-tdd.md) (benchmarks avant release, pas en nightly ; régression > 20 % = bloquant release), [ADR-0004](../adr/0004-sqlite-wal.md). Mis en place en M5 L15.

## La mesure : `bench_reader_page_turn`

Le lecteur d'images (M5 L10) précharge un nombre borné de pages devant celle affichée. L'exigence ne budgete pas l'ouverture du livre ni un tour qui attend le réseau : elle budgete **le tour de page depuis le cache** — entre l'action « page suivante » et la page suivante affichée, décodée.

Le bench ([`e2e/bench/bench_reader_page_turn.spec.ts`](../../e2e/bench/bench_reader_page_turn.spec.ts)) ouvre une Unité d'images synthétique dans la SPA réelle servie par l'API réelle (base vide ; le livre est répondu par des routes Playwright, comme [`e2e/tests/m5_reader_images.spec.ts`](../../e2e/tests/m5_reader_images.spec.ts) : ce qu'un navigateur doit dessiner est le tour, le reste du lecteur est prouvé côté serveur par les tests d'intégration). Puis, tour après tour :

1. il attend que le préchargement ait demandé toute sa fenêtre (3 pages devant, la constante du moteur) et laisse le corps atterrir en main ;
2. il arme l'horloge **dans la page** (`performance.now()`, écouteur de capture posé avant celui du lecteur) ;
3. il appuie sur « page suivante » (flèche droite) et arrête l'horloge quand l'image à l'écran est la page suivante, décodée (`naturalWidth > 0`).

**30 tours** sont mesurés ; le p50 et le p95 (rang le plus proche, comme les benches backend : jamais une moyenne de deux échantillons, un tour lent n'est jamais lissé) vont en JSON dans `test-results/bench_reader_page_turn.json`.

### Fixture et extrapolation (ENF-PERF-008)

Le profil de référence (ADR-0004) est de 2 000 Œuvres / 60 000 Unités / 3 M pages. Le bench ne le reproduit pas : il mesure un tour de page, qui ne dépend pas du volume de la bibliothèque mais de la page elle-même. La page synthétique a **les dimensions d'une page du profil de référence** (1600 × 2400) et une teinte unie : ce que le tour paie est le décodage, pas les octets. Aucun vrai fichier, aucun réseau sortant. L'extrapolation documentée : le décodage est le poste dominant d'un tour depuis le cache ; la lecture sur le disque et la contention du serveur sont couvertes par les autres benches (ENF-PERF-001, -004, -005, -007, -009) sur le profil de référence.

### Le seuil

**p95 ≤ 150 ms** sur 30 tours. Le bench échoue au-delà. Le seuil vient de la spécification ; une comparaison ne l'assouplit jamais.

### Preuve que le bench mesure ce qu'il prétend

À sa première écriture, le bench pressait « page suivante » dès que la page précédente était dessinée, sans laisser le préchargement remplir le cache : les tours qui rencontraient une page encore en vol attendaient le réseau et le p95 mesuré était de **370 ms** (max 558 ms) — au-dessus du budget. Le bench attrape un tour qui attend le réseau ; c'est la preuve que la mesure est réelle. Avec l'attente du cache, le p95 mesuré est de l'ordre de **40 à 50 ms**.

## La règle des 20 % (bloquant release)

Les benchmarks se mesurent **avant une release, jamais en nightly** (03 §4). La CI publie le résultat **en rapport seul** (job `reader-bench`, `continue-on-error`) : un runner bruyant ne bloque jamais une PR.

La porte de release compare le résultat frais au **dernier résultat enregistré** ([`e2e/bench/reference.json`](../../e2e/bench/reference.json), versionné parce que les artefacts de workflow expirent) :

```sh
cd e2e && npm run test:bench
python3 bench/compare.py test-results/bench_reader_page_turn.json
```

`compare.py` bloque (code 1) si le p95 dépasse le budget de la spécification, **ou** s'il régresse de plus de **20 %** sur le p95 enregistré. La référence se réenregistre **à dessein** — jamais automatiquement — dans la PR de release, sur le lanceur qui mesure les releases ; si l'environnement de mesure change, la référence est réenregistrée avant toute comparaison, comme la référence des écrans clés ([`e2e/README.md`](../../e2e/README.md)). La marche à suivre est dans la liste de release ([`docs/releasing.md`](../releasing.md)).

Le p95 varie avec la charge du lanceur (de ~36 ms au repos à ~96 ms observé sur une machine chargée, toujours sous le budget) : la comparaison se fait entre deux runs du même lanceur dans des conditions comparables, et un run visiblement bruité se relance avant de bloquer quoi que ce soit.

## Comment la lancer

```sh
cd e2e
npm ci && npx playwright install chromium   # une fois
npm run test:bench                            # projet `bench` à part
```

Le projet `bench` (`playwright.bench.config.ts`) a son propre serveur (port 8793, base `.tmp-bench`) et **n'entre jamais** dans les e2e habituels (`playwright test` lit `tests/`), ni dans les parcours de l'image, ni dans la nightly.
