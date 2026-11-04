---
status: accepté
date: 2026-11-02
amended: 2026-11-02 (passe 1) ; 2026-11-04 (porteur 2)
decision-makers: mainteneur
---
# ADR-0002 — Langage et runtime : Python 3.13+ / FastAPI en bordure

> **Amendée le 2026-11-02 (passe 1).** En cas de divergence, la section « Amendement passe 1 » prévaut sur le texte d'origine, conservé pour l'historique.

## Contexte et problème
Le produit est surtout de l'acquisition (scraping, colle vers des méta-sources), de la normalisation et de l'empaquetage (CBZ, EPUB). Il doit tenir sous 500 Mo de RAM au repos (ENF-PERF-001), être maintenable par une seule personne et accessible aux auteurs de plugins.

## Facteurs de décision
- Écosystème de scraping et d'impersonation TLS.
- Réutilisation de bibliothèques Python (EPUB, image) et adaptateur vers l'instance lncrawl de l'utilisateur.
- Facilité d'écriture d'un plugin (un fichier).
- RAM au repos, typage, distribution.

## Options considérées
### A. Python 3.13+ (FastAPI, pydantic v2, uv)
- Avantages : meilleur écosystème (curl_cffi, selectolax, lxml, Playwright/Camoufox) ; lncrawl est en Python ; un plugin = un fichier `.py` ; ~80–150 Mo au repos (valeur typique, non mesurée).
- Inconvénients : typage à l'exécution plus faible (`ty` encore en 0.0.x) ; traitement d'image CPU-bound ; pas de binaire unique.
### B. TypeScript (Node/Bun + Hono/Fastify)
- Avantages : une seule langue avec le front, schémas zod partagés, sharp excellent, `bun build --compile`.
- Inconvénients : impersonation TLS moins mûre ; perd lncrawl et gallery-dl en processus.
### C. Kotlin/JVM
- Avantages : les extensions Mihon/keiyoushi tourneraient nativement.
- Inconvénients : il faudrait refaire le shim Android de Suwayomi ; 300–600 Mo+ de RAM ; Gradle rebute les contributeurs.
### D. Go / Rust
- Avantages : RAM minimale, binaire unique.
- Inconvénients : écosystème EPUB et scraping plus mince ; plugins hors processus ou WASM obligatoires ; barrière haute pour les contributeurs.

## Décision
Option A (D1) : Python 3.13+, FastAPI uniquement dans les adaptateurs `interfaces/`, pydantic v2, uv. Litestar reste une alternative : comme FastAPI est confiné en bordure, la bascule coûterait quelques jours. Kotlin et TypeScript sont rejetés après l'exercice d'avocat du diable de 04-backend : l'écosystème manga Kotlin est consommé via Suwayomi (ADR-0008).

## Conséquences
- Positives : réutilisation directe de lncrawl, curl_cffi et ebooklib ; plugins simples à écrire ; RAM dans le budget.
- Négatives : deux langages (Python + TS front) ; typage strict à imposer (mypy/pyright) ; traitements d'image à isoler dans des workers.

## Risque principal
Dette de typage et performance CPU (images). Mitigation : pydantic aux frontières, vérificateur de types en CI, pyvips/Pillow avec plafonds (ENF-SEC-003).

## Réversibilité
Faible : changer de langage revient à réécrire. Le contrat de plugin hors processus (ADR-0007) limite l'impact sur les plugins.

## Amendement passe 1 (2026-11-02)
- **Mention retirée** (A17) : le facteur « réutilisation » ne cite plus d'outil lié aux achats en ligne ; seuls restent les bibliothèques EPUB/image et l'adaptateur vers l'instance lncrawl de l'utilisateur (A19).
- **RAM mesurée** (09-closure-B) : prototype à 83 Mo au repos et 155 Mo après traitement d'images. Budget ENF-PERF-001 : ≤ 250 Mo au repos, ≤ 500 Mo en pic hors services tiers ; atténuations `MALLOC_ARENA_MAX=2`, cache vips plafonné, imports paresseux. Image de base `python:3.13-slim` (~43 Mo). Repères des pairs : Mealie ~270–400 Mo, Paperless 400–550 Mo, Kavita ~200 Mo avec pics à 2–2,7 Go, Suwayomi ≥ 750 Mo.
- **Fetcher V1 minimal** (A13) : httpx (ou le fork httpx2) + `httpx-secure` + limitation de débit + cookies. curl_cffi, solveur et navigateur headless ne sont introduits qu'en M2+ pour une source native qui l'exige, derrière la même garde SSRF (ENF-SEC-001).
- Le choix Python est inchangé.

## Amendement porteur 2 (2026-11-04)
- **Choix confirmé par le porteur** (`review/ARBITRAGES-porteur2.md`, Q1) après une nouvelle comparaison avec une stack 100 % JavaScript/TypeScript. **Express** : vieillissant ; en TypeScript, Hono ou Fastify (option B) seraient retenus. **Next.js plein cadre** : écarté même dans un monde TypeScript : modèle pensé pour le rendu serveur et l'hébergement géré, **sans file de tâches de fond** (téléchargements, exports, scans exigeraient un second processus, donc plus de monolithe simple), et faille d'authentification par middleware contournable par un en-tête (CVE-2025-29927, critique) qui illustre le risque d'une auth placée dans une couche de routage. L'argument décisif reste l'écosystème d'acquisition et de formats (curl_cffi, lncrawl, gallery-dl, FanFicFare, lxml/defusedxml, Calibre) et les plugins en un fichier `.py`. Le partage des types avec le front est couvert par le client TypeScript généré depuis l'OpenAPI (ADR-0006).

## Exigences liées
ENF-PERF-001, ENF-PERF-002, ENF-PERF-009, ENF-SEC-001, EF-SRC-001, EF-SRC-010.

## Sources
- https://github.com/lncrawl/lightnovel-crawler
- https://github.com/lexiforest/curl_cffi
- https://github.com/rushter/selectolax
- https://github.com/fastapi/fastapi
- https://github.com/litestar-org/litestar
- https://github.com/Suwayomi/Suwayomi-Server
- https://github.com/mealie-recipes/mealie/issues/5417
- https://github.com/paperless-ngx/paperless-ngx/issues/3616
- https://github.com/Kareadita/Kavita/issues/1623
- https://github.com/Suwayomi/Suwayomi-Server/issues/2353
- https://hub.docker.com/_/python
- https://github.com/Zaczero/pkgs/tree/main/httpx-secure
- https://github.com/pydantic/httpx2
