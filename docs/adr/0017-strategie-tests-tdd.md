---
status: accepté
date: 2026-11-02
amended: 2026-11-02 (passe 1) ; 2026-11-03 (passe 11) ; 2026-11-03 (passe 12)
decision-makers: mainteneur
---
# ADR-0017 — Stratégie de tests : TDD strict et pyramide adaptée au scraping

> **Amendée le 2026-11-02 (passe 1).** En cas de divergence, la section « Amendement passe 1 » prévaut sur le texte d'origine, conservé pour l'historique.

## Contexte et problème
Les sites cassent chaque semaine, les formats de sortie doivent être exacts (Kavita, EPUBCheck, XSD ComicInfo), le mainteneur est seul et s'appuie sur des assistants IA, qui ont tendance à écrire des tests qui n'affirment rien. Les extensions keiyoushi sont pour la plupart non testées : c'est un écueil à éviter.

## Facteurs de décision
- Tests rapides et déterministes en PR (aucun réseau).
- Détection précoce des sites cassés, sans bloquer les PR.
- Exactitude des sorties face aux serveurs réels.
- Honnêteté du TDD avec l'IA.

## Options considérées
### A. Pyramide dédiée
- Contenu : parseurs purs sur fixtures ; HTTP enregistré (pytest-recording, `record_mode=none`) ; kit de conformité plugins avec `TESTS` par source (modèle yt-dlp) ; golden files + EPUBCheck + XSD ; Hypothesis ; Vitest + MSW ; Playwright sur l'image construite ; Kavita réel en nightly ; canaris nocturnes non bloquants ; mutation testing hebdomadaire ; couverture diff ≥ 90 %.
- Avantages : couvre chaque classe de risque ; les PR ne dépendent jamais d'Internet.
- Inconvénients : beaucoup d'outillage dès M0.
### B. Tests unitaires + tests live
- Avantages : simple.
- Inconvénients : PR instables à cause des sites ; aucune garantie sur les sorties.
### C. E2E seulement
- Avantages : réaliste.
- Inconvénients : lent, fragile, diagnostic difficile.

## Décision
Option A (D26).
- **Règles TDD** : test rouge écrit et commité d'abord (si possible dans un commit séparé), puis vert, puis refactor.
- **Règles IA** (AGENTS.md / AI_POLICY.md) : jamais modifier un test pour le faire passer sans le dire ; un humain comprend chaque changement.
- **Mises à jour de snapshots interdites en CI**.
- **Avertissement** (contournable par label) si une PR touche `backend/src` sans toucher `tests/`.
- **Couverture** : gate diff ≥ 90 %, plancher global ≥ 80 % avec branches.
- **Canaris** : ils ouvrent ou mettent à jour une issue `source-broken` par source, sans jamais bloquer une PR.

## Conséquences
- Positives : régressions de format détectées en PR ; casse de site détectée la nuit ; tests IA vides repérés par mutation testing.
- Négatives : coût initial en M0/M1 ; cassettes à rafraîchir et à nettoyer (cookies, jetons scrubbés).

## Risque principal
Les cassettes s'éloignent de la réalité (tests verts, site cassé) (R2). Mitigation : les canaris rejouent les mêmes `TESTS` en live chaque nuit.

## Réversibilité
Haute : chaque étage est indépendant.

## Amendement passe 1 (2026-11-02)
- **Couverture** (A16, SIM-14) : gate **diff ≥ 85 %**, plancher **global ≥ 80 %** (au lieu de 90 %). Le mutation testing devient **optionnel, mensuel** ; les benchmarks se font avant release, sur le profil de référence (ENF-PERF-008).
- **Fixtures et cassettes synthétiques uniquement** (A18, ENF-LEG-002) : images générées (manifest de hash vérifié en CI), domaine public, serveurs simulés (Suwayomi, OPDS reproduisant Kavita/Komga) ; gitleaks et liste blanche d'en-têtes sur `cassettes/` ; CODEOWNERS.
- **Canaris** (A19, ENF-LEG-003) : seulement sur des sources dont les conditions l'autorisent, jamais avec impersonation ni solveur, **jamais contre gutenberg.org** (catalogues RDF/CSV ou miroirs).
- **EPUBCheck** n'est requis que si une génération EPUB maison existe (réservée, EF-EXP-012 retirée) ; ComicInfo est validé contre la XSD.
- **Vecteurs de test du hash KOReader** (closure-A, ENF-QUAL-005) : partialMD5 sur fichiers de 0, 1023, 1024, 1989, 4095, 4096, 4097 octets et > 1 Gio (creux) ; hex minuscule ; lectures courtes hachées partiellement (régression connue chez un pair) ; compatibilité avec Kavita (majuscules, 11 échantillons, divergence > 1 Gio).
- **Nouveaux types de tests** : E2E chronométré « 10 premières minutes » et test d'utilisabilité 5 tâches (ENF-UX-001) ; simulateur de pannes des dépendances (ENF-OPS-011) ; test de restauration de sauvegarde en CI (ADR-0023) ; test de propriétés `openat2` contre symlinks plantés (ENF-SEC-012).

## Amendement passe 11 (2026-11-03)
L3 (GAP-11-04). Les tests de contrat contre un Suwayomi réel (spike S5 local, nightly) n'utilisent que la **source locale** de Suwayomi et des **CBZ synthétiques** du dépôt, **aucun dépôt d'extensions**, **aucune sortie réseau** (ENF-LEG-013). Rien de ce qui entre dans le dépôt ne provient d'une extension tierce ; le corpus d'étiquettes de langue relevé en S5 n'est versé que réduit aux étiquettes (lint `denylist-names`). Les tests des passes 11 sont écrits avant le code à leur jalon (voir `03-roadmap-tdd.md`).

## Exigences liées
ENF-QUAL-001, ENF-QUAL-003, ENF-QUAL-004, ENF-QUAL-005, ENF-COMP-001, ENF-LEG-002, ENF-LEG-003, ENF-UX-001, ENF-OPS-011, EF-SRC-014, EF-EXP-011, EF-EXP-013, ENF-LEG-013, EF-SRC-008, ENF-I18N-005.

## Sources
- https://github.com/yt-dlp/yt-dlp/tree/master/.github/workflows
- https://github.com/w3c/epubcheck
- https://github.com/anansi-project/comicinfo
- https://github.com/keiyoushi/extensions-source
- https://github.com/home-assistant/core/blob/dev/AI_POLICY.md
- https://github.com/mealie-recipes/mealie
- https://github.com/koreader/koreader/blob/master/frontend/util.lua
- https://github.com/gotson/komga/commit/a5462b0650fd573a7ec72a734ea2ba9e8842b8bf
- https://github.com/Kareadita/Kavita/blob/develop/Kavita.Services/Helpers/KoreaderHelper.cs
- `deliverables/review/chasseur-trous-passe11.md`, `deliverables/review/explorateur-passe11.md`, `deliverables/review/ARBITRAGES-passe11.md` (passe 11)

## Amendement passe 12 (2026-11-03)
M9, M10 (AA3, AA6, GAP-12-10).

- **Oracle `plugin/parse`** : il valide le **nom** seulement ; le golden `test_chapter_name_has_no_volume_golden_plugin_parse` montre que le nom ne porte pas de tome ; le rangement (série, tome, numéro) est vérifié par `test_nightly_real_kavita_places_chapter_volume_from_comicinfo` contre un Kavita réel.
- **Version minimale testée** : la cause d'AA1 (route absente de la version minimale passée inaperçue) est fermée par la matrice {minimum, épinglée, dernière} et le test de routes contre l'`openapi.json` de chaque version (ADR-0016).
- **Limites de test écrites** : I42 (stabilité de `sourceId` d'une extension) vérifié par le contrat, des faux et l'extension synthétique, hypothèse surveillée par `source.renumbered` ; I43 par une version annoncée simulée ; I46 par l'absence d'écriture (base, fichier, journal), le swap et les vidages mémoire étant hors de portée (`RLIMIT_CORE=0` dans l'image).

Source : `deliverables/review/chasseur-trous-passe12.md`, `deliverables/review/explorateur-passe12.md`, `deliverables/review/ARBITRAGES-passe12.md` (passe 12)
