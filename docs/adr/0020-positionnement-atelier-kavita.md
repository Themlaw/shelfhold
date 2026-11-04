---
status: accepté
date: 2026-11-02
amended: 2026-11-02 (passe 2) ; 2026-11-03 (passe 12)
decision-makers: mainteneur
---
# ADR-0020 — Positionnement : Shelfhold est l'atelier, Kavita est le lieu de lecture

> **Amendée le 2026-11-02 (passe 2).** En cas de divergence, la section « Amendement passe 2 » prévaut sur le texte qui précède.

## Contexte et problème
La version 1 des livrables plaçait le lecteur intégré, l'OPDS, kosync, OIDC et la restriction d'âge dans V1 (M3–M4). Shelfhold devenait ainsi un second Kavita. La revue (OBJ-SIM-01, OBJ-UX-01, arbitrages A1–A3) a relevé quatre choses :
- le propriétaire lit déjà dans Kavita via Kover, et KOReader dans Kavita via kosync ;
- dupliquer lecture, progression, OPDS et contrôle parental double la surface de bugs pour un mainteneur seul ;
- les deux lieux de lecture embrouillent la famille (« où j'en suis ? ») ;
- la valeur que personne d'autre n'offre est **trouver, demander, récupérer, ranger et exporter** proprement vers Kavita.

## Facteurs de décision
- Délai jusqu'à une version utile (pont Suwayomi → Kavita en M1).
- Un seul endroit où lire pour la famille en V1, et une seule progression.
- Pas de régression pour le propriétaire (Kover, KOReader, Kavita existants).
- Conserver la possibilité d'un lecteur propre plus tard sans refonte du schéma.

## Options considérées
### A. Shelfhold = atelier ; Kavita = lieu de lecture principal ; lecteur intégré secondaire en M5
- Avantages : V1 courte et centrée sur une valeur unique. Kavita fournit la lecture, la progression, l'OPDS, kosync, la restriction d'âge et les comptes. Pas de concurrence entre deux progressions en V1.
- Inconvénients : dépendance forte à Kavita, qui doit être compatible et accessible. Le contrôle parental de la lecture dépend de l'`AgeRating` exporté. Pas d'« extrait » avant M5.
### B. Shelfhold = application complète (atelier + lecteur + OPDS + kosync) dès V1
- Avantages : une seule application ; utilisable sans Kavita.
- Inconvénients : V1 deux fois plus longue. Duplique Kavita et sa progression, ce qui impose une synchronisation bidirectionnelle dès V1. Surface de sécurité accrue (contenu non fiable servi au navigateur).
### C. Shelfhold = simple script d'export sans UI
- Avantages : minimal.
- Inconvénients : pas de mode famille « Demander », pas de recherche unifiée, pas d'adoption guidée. Ne répond pas aux objectifs O1–O8.

## Décision
Option A.
- **V1 (M0–M3, tag 1.0)** : atelier = sources, recherche unifiée, demandes, file d'activité, étagères, export vers Kavita, adoption de la bibliothèque Kavita existante.
- **Délégation à Kavita en V1** :
  - lecture (Kover et web) ;
  - OPDS ;
  - kosync ;
  - restriction d'âge de lecture (via `AgeRating` dans ComicInfo, EF-EXP-011) ;
  - comptes de lecture.
- **Plus tard** :
  - M4 : synchronisation de progression avec Kavita (EF-EXP-026) ;
  - M5 : lecteur intégré secondaire (aperçu, lecture d'œuvres non exportées), OPDS et kosync propres, OIDC, page `/lite`.

Le vocabulaire et les verbes UI suivent ce choix : « Ouvrir dans Kavita » est obligatoire ; « Exporter » n'est jamais un verbe exposé à la famille.

## Conséquences
- Positives : M1 livre le pont Suwayomi → Kavita. La 1.0 est atteignable. La famille garde un seul lieu de lecture. Le modèle de progression par locator (crochet de schéma) reste présent pour M4–M5.
- Négatives : compatibilité Kavita critique (ENF-COMP-001, plage supportée, mode dégradé). Le contrôle parental de Shelfhold couvre ses surfaces propres ; la lecture dépend de Kavita. Les utilisateurs sans Kavita attendront M4 (dossier natif) ou M5 (lecteur).

## Risque principal
Un changement d'API ou de scanner Kavita casse le pont. Mitigations :
- lecture de version au démarrage ;
- mode dégradé « plan seulement » hors plage (nommé `degraded_readonly`, aperçu seulement, depuis la passe 13) ;
- nightly sur version épinglée + `latest` non bloquant ;
- correspondance de chemins plutôt que dépendance au compose Kavita (ADR-0022).

## Réversibilité
Moyenne. Le lecteur peut être avancé (le module front est isolé, le schéma de progression existe). Revenir à l'option B coûterait la synchronisation bidirectionnelle.

## Amendement passe 2 (2026-11-02)
- **Revue de chevauchement** (N2, B18, ENF-GOV-003) : à chaque sortie de jalon, les EF restantes sont comparées aux notes de version de Kavita (et Komga) ; une fonction que Kavita livre passe en « Jamais » ou est réévaluée.
- Conséquences immédiates : **statistiques de lecture (EF-USR-009) → Jamais** (Kavita 0.8.9 a des statistiques par sessions avec partage opt-in) ; **sync Kobo (Q21) → laissée à Kavita** (axe annoncé pour 2026), Shelfhold ne l'émule en M6 que si Kavita ne la livre pas d'ici là.
- **Version minimale de Kavita : 0.9.1** (relevée de 0.8.9 en passe 12, voir l'amendement passe 12 ; clés nommées à expiration) ; `known_bad_versions` → avertissement à l'export ; la version minimale figure dans chaque note de version.
- **Contrôle parental** : dans Kavita, la barrière est l'accès par bibliothèque (ADR-0027).

## Amendement passe 4 (2026-11-03)
Revue de chevauchement (D13, R7) : elle couvre aussi les notes de version de **Suwayomi** et un tour des **concurrents émergents**, en plus de Kavita et Komga ; la même revue de jalon publie la vélocité et applique la règle de coupe (ADR-0035).

## Exigences liées
EF-EXP-009, EF-EXP-011, EF-EXP-026, EF-EXP-028, EF-UX-004, EF-UX-006, EF-USR-004, EF-USR-006, EF-LEC-001, EF-LEC-003, EF-LEC-009, EF-LEC-010, ENF-COMP-001, ENF-GOV-003, ENF-GOV-005.

## Sources
- https://wiki.kavitareader.com/guides/features/opds/
- https://wiki.kavitareader.com/guides/scanner/
- https://github.com/Kareadita/Kavita/blob/develop/Kavita.Server/Controllers/KoreaderController.cs
- https://github.com/Kareadita/Kavita/blob/develop/Kavita.Server/Controllers/PluginController.cs
- https://github.com/anansi-project/comicinfo (champ `AgeRating`)
- `deliverables/review/ARBITRAGES-passe1.md` (A1–A3), OBJ-SIM-01, OBJ-UX-01
- `deliverables/review/explorateur-passe4.md` (R7)
- `deliverables/review/ARBITRAGES-passe4.md`

## Amendement passe 12 (2026-11-03)
M9 (AA1, AA2, AA3, AA6).

- **Version minimale de Kavita : 0.9.1** pour toutes les étagères (remplace 0.8.9) : `GET /api/plugin/parse` et `POST /api/Plugin/parse-bulk`, dont dépendent la détection de collision, l'adoption et la sonde dès M1a, n'existent qu'à partir de 0.9.0, et 0.9.1 corrige les failles `auth` et `access_control` connues.
- L'API Kavita n'a ni préfixe de version ni politique de dépréciation (26 routes retirées entre 0.8.9.1 et 0.9.1.4) : la liste blanche des routes est écrite dans `docs/kavita-routes.md` (route, méthode, usage, jalon), testée hors ligne contre l'`openapi.json` figé de chaque version de la plage ; nightly sur {minimum, épinglée, dernière} ; un 404 sur une route de la liste met la destination en mode dégradé (`degraded_feature`, `target.route_missing`, depuis la passe 13), jamais de plan sans contrôle de collision ; Kavita de test amorcé sans interface (fixture par version).
- `plugin/parse` ne lit que le nom (ni ComicInfo ni repli sur le dossier) : il valide le nom ; le rangement est validé contre un Kavita réel en nightly (O3 reformulé).

Source : `deliverables/review/chasseur-trous-passe12.md`, `deliverables/review/explorateur-passe12.md`, `deliverables/review/ARBITRAGES-passe12.md` (passe 12)

## Amendement passe 13 (2026-11-03)
N3, N4 (GAP-13-03, GAP-13-11).

- **Propriétaire des champs (N3)** : Shelfhold tient un registre `field_owner` des champs qu'il a lui-même verrouillés dans Kavita ; il ne réécrit que ceux-là (âge de série des bibliothèques gérées inclus) et jamais un champ verrouillé par un tiers ; un écart avec la dernière valeur poussée émet `reconcile.drift` ; après une réinstallation de Kavita, `target reconnect` repousse les âges classés. `docs/admin/proprietaire-des-champs.md` le dit.
- **Modes dégradés (N4)** : `degraded_readonly` (version hors plage, `target.version_unsupported`, aperçu seulement) et `degraded_feature` (404 sur une route de la liste, `target.route_missing`, fonctions dépendantes coupées, aucun plan appliqué sans contrôle de collision, sortie `target recheck`). Les mentions « mode dégradé » de cette ADR se lisent ainsi.
- Tests : `test_kavita_field_rewritten_only_if_owned_by_shelfhold`, `test_kavita_reconnect_repushes_rated_series_ages`, `test_kavita_route_404_sets_degraded_mode_never_plans_without_collision_check`.

Source : `deliverables/review/chasseur-trous-passe13.md`, `deliverables/review/explorateur-passe13.md`, `deliverables/review/ARBITRAGES-passe13.md` (passe 13)
