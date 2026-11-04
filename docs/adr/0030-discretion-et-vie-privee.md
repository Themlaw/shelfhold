---
status: accepté
date: 2026-11-02
amended: 2026-11-03 (passe 6) ; 2026-11-03 (passe 7) ; 2026-11-03 (passe 8) ; 2026-11-03 (passe 11) ; 2026-11-04 (arbitrage porteur 1)
decision-makers: mainteneur
---
# ADR-0030 — Discrétion de l'instance et vie privée entre membres du foyer

## Contexte et problème
Deux angles morts sont apparus en passe 2 :
- **Discrétion** (N4) : les membres passent par une instance exposée sur Internet (par un reverse proxy quelconque, par exemple Pangolin ; voir l'amendement porteur 1). Rien n'empêche l'indexation par les moteurs de recherche, ni l'**aperçu de lien** (Open Graph) quand un membre colle un lien dans une messagerie, ni une page de connexion qui affiche des couvertures ou des noms de bibliothèques. Cela expose le propriétaire à un signalement ou à un ciblage.
- **Vie privée entre adultes** (N8) : l'admin voit l'Activité, les étapes des jobs et « Mes demandes » de chacun. Une demande d'un conjoint ou les lectures d'un adolescent sont visibles en clair. Ajouter une notion de visibilité après coup obligerait à revoir toutes les requêtes d'Activité, de notifications et d'historique, avec un risque de fuite par les notifications admin.

## Facteurs de décision
- Coût quasi nul s'il est posé en M1 ; coûteux après.
- Une seule fonction de projection, testable par table de vérité, plutôt que des filtres dispersés.
- Le contrôle parental prime pour les profils enfant.
- L'admin doit pouvoir diagnostiquer une panne sans lire les titres.

## Options considérées
### A. Rien de spécifique : authentification seulement, l'admin voit tout
- Avantages : simple.
- Inconvénients : indexation et aperçus possibles avant connexion ; aucune intimité entre adultes.
### B. En-têtes et page neutre (discrétion) + champ `visibility` et fonction unique `activity_view(viewer, event)` (vie privée)
- Avantages : quelques lignes de middleware ; un seul point de vérité pour l'Activité, le SSE, les notifications et le résumé ; démasquage explicite et journalisé.
- Inconvénients : un champ de plus sur les demandes et les événements ; l'admin voit des titres masqués.
### C. Chiffrement de bout en bout des demandes par membre
- Avantages : intimité forte.
- Inconvénients : incompatible avec l'approbation par l'admin et le diagnostic ; complexité hors de portée.

## Décision
Option B (arbitrages B20, B22).
- **Discrétion** (ENF-PRIV-002, M1) : `X-Robots-Tag: noindex, nofollow` sur toutes les réponses ; `robots.txt` « Disallow: / » ; aucune balise Open Graph ni Twitter Card ; page de connexion neutre ; toutes les routes de contenu authentifiées. Le middleware existant (CSP) porte ces en-têtes ; un test vérifie chaque route.
- **Visibilité** (EF-USR-016, modèle en M1, réglage UI en M3) : `visibility` (`private` | `household`) sur les demandes et les événements ; défaut `private` pour un adulte, `household` pour un profil enfant. `activity_view(viewer, event)` est la **seule** projection (notifications comprises). L'admin voit les opérations avec le titre masqué pour les demandes privées d'autres adultes ; le démasquage est explicite et journalisé (`audit`).
- **Vocabulaire** (ENF-LEG-006, B21) : « bibliothèque personnelle du foyer », jamais « partager » au sens de diffuser.

## Conséquences
- Positives : instance invisible des moteurs et des aperçus ; intimité entre adultes sans perte de diagnostic ; une seule fonction à tester.
- Négatives : l'admin doit démasquer (journalisé) pour certains diagnostics ; les tests de projection deviennent une table de vérité à maintenir.

## Risque principal
Une surface (nouvelle notification, export JSON) contourne `activity_view`. Mitigation : règle import-linter (les consommateurs d'événements ne lisent jamais la table `event` directement) et test paramétré sur toutes les surfaces, comme pour le contrôle parental (ENF-SEC-010).

## Réversibilité
Haute : en-têtes et valeur par défaut de `visibility` sont des réglages.

## Amendement passe 3 (2026-11-02)
- **Rétention et pseudonymisation** (C13) : événements et jobs 90 j, audit 1 an ; la suppression d'un utilisateur pseudonymise l'audit (jeton opaque) au lieu de le détruire (EF-USR-013).
- **Vignettes privées** (GAP-3-13) : `Cache-Control: private`, `Vary: Cookie`, autorisation avant le calcul du 304 (EF-BIB-020).
- **Assistants IA** (P14, plus tard) : clés API à portée (`read`, `request`, `admin`) et champs amont marqués « non fiables » dans l'OpenAPI.

## Amendement passe 4 (2026-11-03)
Discrétion (D1, D4) : les en-têtes de discrétion et la page de connexion neutre sont livrés en **M1a** avec le socle d'authentification ; le modèle `visibility` et `activity_view` en M1b. Les textes amont affichés et notifiés sont assainis (ENF-SEC-015).

## Amendement passe 6 (2026-11-03)
GAP-6-16, GAP-6-22.
- **Aucun identifiant Shelfhold n'est inscrit dans les fichiers exportés** (ni `<Notes>` ComicInfo, ni `shelfhold:id` OPF) : un fichier peut quitter le foyer, il ne doit pas porter la trace de l'outil ; la reconnaissance après restauration ou déplacement repose sur le manifeste, le hash et le chemin (ADR-0040).
- Le **paquet de diagnostic** masque les domaines, les `manga.url` et les noms de sous-sources (remplacés par `<source-1>`, `<source-2>`…), en plus d'exclure les fichiers tatoués (ENF-LEG-009) ; test `test_diagnostic_bundle_redacts_source_urls`.

## Amendement passe 7 (2026-11-03)
G10 (GAP-7-16).
- Toute charge utile sortante (webhooks, outbox de M4) passe par `activity_view` avec un rôle « intégration » de la table de vérité : le titre d'une demande privée n'en sort jamais (EF-INT-005, EF-USR-016).

## Amendement passe 8 (2026-11-03)
H1 (GAP-8-01).
- La règle « aucun identifiant dans les fichiers exportés » est **confirmée** : la lignée nécessaire à la réadoption vit dans le **miroir de provenance** de chaque racine physique (`.shelfhold/manifest/`, ADR-0046), dossier caché exclu de Kavita, jamais copié vers une destination `egress = externe`, jamais placé dans un paquet de diagnostic, sans URL ni domaine de source (I33).
- ComicInfo `Translator` = libellé d'Équipe : métadonnée publique, déjà visible dans la source ; `Web` reste vide par défaut (option de l'étagère), pour ne pas inscrire d'URL de source dans un fichier qui peut quitter le foyer.

## Amendement passe 9 (2026-11-03)
J2, J5 (GAP-9-15).
- `.shelfhold/` (miroir de provenance, pierres tombales) est créé en `0700`/`0600` quand l'uid le permet ; Kavita n'a pas besoin de le lire ; la doc « topologie type » prévient que les partages du dossier et les sauvegardes de l'hôte l'embarquent.
- Pierres tombales et audit de purge ne gardent ni titre ni référence lisible (passe 9 : empreinte salée par l'identifiant d'instance ; passe 11 : valeurs chiffrées, rien de lisible sans le trousseau).

## Amendement passe 11 (2026-11-03)
L1 (GAP-11-02). Les pierres tombales ne gardent plus d'empreinte salée par l'identifiant d'instance (public dans `.shelfhold-mount-id`, donc vulnérable à une attaque par dictionnaire ou par confirmation) ni de `content_hash` en clair : leurs valeurs sont **chiffrées** (AEAD, clé dérivée du trousseau) et rien n'est lisible sans le trousseau ; l'index déchiffré vit en mémoire seulement (`test_tombstone_values_encrypted_not_dictionary_reversible`). La purge réécrit aussi `store/INDEX.jsonl` (GAP-11-08).

## Exigences liées
ENF-PRIV-001, ENF-PRIV-002, EF-USR-016, EF-INT-021, ENF-OBS-003, ENF-LEG-006, ENF-SEC-010, EF-USR-013, EF-BIB-020, ENF-OPS-009, ENF-SEC-015, ENF-LEG-009, EF-EXP-058, EF-INT-005, EF-EXP-063, EF-BIB-052, EF-BIB-054.

## Sources
- https://developers.google.com/search/docs/crawling-indexing/block-indexing
- https://ogp.me/
- https://opencollective.com/kavita/updates/v0-8-9-new-stats-pages-journal-style-reading-50x-faster-scanner-and-so-much-more (partage de statistiques opt-in, visibilité admin de l'activité chez Kavita)
- `deliverables/review/explorateur-passe2.md` (N4, N7, N8), `deliverables/review/ARBITRAGES-passe2.md` (B20–B22)
- `deliverables/review/chasseur-trous-passe3.md` (GAP-3-13, GAP-3-26)
- `deliverables/review/chasseur-trous-passe4.md` (GAP-4-01, GAP-4-11)
- `deliverables/review/ARBITRAGES-passe4.md`
- `deliverables/review/chasseur-trous-passe7.md`, `deliverables/review/explorateur-passe7.md`, `deliverables/review/ARBITRAGES-passe7.md` (passe 7)
- `deliverables/review/chasseur-trous-passe8.md`, `deliverables/review/explorateur-passe8.md`, `deliverables/review/ARBITRAGES-passe8.md` (passe 8)
- `deliverables/review/chasseur-trous-passe9.md`, `deliverables/review/explorateur-passe9.md`, `deliverables/review/ARBITRAGES-passe9.md` (passe 9)
- `deliverables/review/chasseur-trous-passe11.md`, `deliverables/review/explorateur-passe11.md`, `deliverables/review/ARBITRAGES-passe11.md` (passe 11)

## Amendement arbitrage porteur 1 (2026-11-04)
P5. Le contexte « instance exposée sur Internet (Pangolin) » est généralisé : l'instance **peut** être exposée sur Internet par n'importe quel reverse proxy ou tunnel, ou rester sur le réseau domestique ; aucun proxy n'est supposé. Les mesures de discrétion (ENF-PRIV-002) sont des protections invisibles et restent actives par défaut (P1).
