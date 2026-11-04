---
status: accepté
date: 2026-11-02
amended: 2026-11-03 (passe 4 ; passe 5) ; 2026-11-03 (passe 6)
decision-makers: mainteneur
---
# ADR-0034 — Bascule depuis une automatisation existante

## Contexte et problème
Le public visé possède souvent déjà une automatisation maison autour de Kavita : Suwayomi qui télécharge **directement** dans les dossiers lus par Kavita (montages de volumes croisés), scripts planifiés qui renomment les fichiers, suppriment les doublons d'équipes ou posent les couvertures par l'API, mise à jour automatique des extensions, filtrage des équipes après coup (P1, P2, P4). Si Shelfhold est installé sans retirer ces écrivains, deux systèmes modifient les mêmes dossiers : un script supprime un doublon, Shelfhold voit un fichier manquant et le réexporte, le script le resupprime — une boucle silencieuse. Le principe d'adoption « sans réécriture » (ADR-0022) ne dit pas comment **reprendre la main**.

## Facteurs de décision
- Jamais deux écrivains sur un dossier géré.
- Bascule réversible, série par série si besoin.
- Documentation générique : aucune donnée propre à une installation dans le dépôt (C14).
- Garder ce que l'automatisation faisait de bien (sonde, préférences d'équipe) en le reprenant dans Shelfhold.

## Options considérées
### A. Cohabitation libre (situation passe 2)
- Avantages : installation immédiate.
- Inconvénients : boucles de réexport, doublons, suppressions croisées, diagnostic difficile.
### B. Détection à l'adoption + procédure de bascule guidée + garde « autre écrivain »
- Avantages : l'assistant voit les signes d'un autre écrivain (fichiers récents au format Suwayomi dans `/library`, couvertures posées hors Shelfhold, renommages hors manifeste) et refuse d'activer le suivi tant que la bascule n'est pas confirmée ; la garde EF-EXP-043 met en pause au lieu de réexporter.
- Inconvénients : une étape de plus à l'installation ; heuristiques de détection imparfaites.
### C. Shelfhold prend le contrôle de force (supprime ou réécrit ce qui ne vient pas de lui)
- Avantages : état propre tout de suite.
- Inconvénients : contraire à « les fichiers inconnus ne sont jamais touchés » (EF-EXP-018) ; risque de perte.

## Décision
Option B (arbitrage C14).
- **Détection** à l'adoption (EF-EXP-044) : montage direct de Suwayomi vers un dossier de Kavita, fichiers récents non issus de Shelfhold, couvertures posées par un tiers ; résultat affiché, suivi refusé tant que l'admin n'a pas confirmé la bascule.
- **Procédure générique** (`docs/admin/migration-depuis-scripts.md`) : 1) sauvegarder ; 2) retirer du compose de Suwayomi les montages qui écrivent dans les dossiers de Kavita (Suwayomi garde son propre dossier de téléchargement, lu par Shelfhold par l'API) ; 3) désactiver, au moment de la bascule, les tâches planifiées qui renomment, dédoublonnent ou posent les couvertures pour les séries que Shelfhold gère ; 4) garder une éventuelle mise à jour automatique des extensions jusqu'à ce que la sonde fonctionnelle (EF-SRC-026) soit en place, puis la retirer ; 5) remplacer le filtrage d'équipes après coup par la préférence d'équipe par série (EF-BIB-035) ; 6) adopter la bibliothèque existante (dry-run d'abord) ; 7) choisir le mode de vérification (`piloted` avec WakeHook, `always_on` sinon, EF-SRC-025) ; 8) passer les redéploiements par l'outil d'orchestration habituel, sans cron ni timer sur l'hôte (ENF-OPS-017).
- **Garde permanente** (EF-EXP-043) : un autre écrivain détecté après la bascule met la série en pause et alerte (`target.foreign_writer_detected`), jamais de réexport en boucle.
- Le plan **propre à une installation** (chemins, hôtes, noms de scripts) n'est jamais versionné ; il vit dans un document privé de l'exploitant.

## Conséquences
- Positives : bascule sûre et réversible ; plus de boucle silencieuse ; documentation réutilisable par tout homelab.
- Négatives : installation plus longue pour les foyers déjà automatisés ; heuristiques à maintenir.

## Risque principal
Un script oublié qui continue d'écrire. Mitigation : garde EF-EXP-043, sentinelle à génération, événement d'exploitation par défaut.

## Réversibilité
Haute **après `shelfhold detach --restore-upstream`** (amendement passe 5) ; remettre les montages et réactiver les scripts ne suffit pas seul, car les séries suivies restent exclues des mises à jour de Suwayomi. Shelfhold n'a rien effacé d'inconnu.

## Amendement passe 4 (2026-11-03)
Fichier adopté de même numéro (D10, GAP-4-08) : par défaut, Shelfhold **s'abstient** : le fichier adopté est conservé et rattaché comme Variante exportée de l'Unité logique ; aucun fichier n'est écrit, aucune traduction n'est changée en cours de série ; le remplacement n'est qu'une proposition (avertissement EF-EXP-037 en M3). L'adoption d'une bibliothèque enfant est refusée en M1a.

## Amendement passe 5 (2026-11-03)
Arbitrages E2, E3, E14 (GAP-5-02, GAP-5-03, T3).
- **Étape « rapatrier »** (EF-EXP-051), insérée **avant** le retrait des montages directs : si des fichiers adoptés vivent hors de `/library` (volume de Suwayomi monté dans Kavita, détecté par `statfs` et `(dev, ino)`), copie **vérifiée par hash** vers le **même chemin relatif** sous `/library`, par le journal d'intentions ; scan Kavita puis contrôle (même nombre de séries et de chapitres, progression intacte) **avant** de désactiver l'ancien chemin ; **dry-run obligatoire**. Si le dossier de téléchargement de Suwayomi est déjà sous `/library`, on change seulement ce dossier dans Suwayomi.
- **Nom de série figé** (EF-EXP-052) : les nouveaux fichiers d'une série adoptée héritent exactement de `Series`, `LocalizedSeries`, `SeriesSort`, langue et dossier des fichiers adoptés ; golden « pas de seconde série Kavita ».
- **Premier cycle** : nouveautés uniquement (EF-EXP-053) ; **préflight des droits** par dossier (EF-EXP-054) ; **audit Kavita** en lecture seule avant toute liaison (EF-EXP-055).
- **Réversibilité corrigée.** La phrase « remettre les montages et réactiver les scripts suffit » est fausse depuis la passe 4 : les séries suivies par Shelfhold sont dans une catégorie Suwayomi exclue des mises à jour globales et la vérification est pilotée par Shelfhold ; arrêter Shelfhold ferait cesser silencieusement l'arrivée des nouveaux chapitres. La réversibilité est **haute après `shelfhold detach --restore-upstream`** (EF-EXP-056, M1a) : séries rendues à la planification de Suwayomi (catégorie et réglage relevés à l'adoption), suivi arrêté sans toucher aux fichiers, liste des dossiers gérés écrite pour les anciens scripts. Test E2E `test_detach_restores_suwayomi_updates_and_leaves_files` dans la DoD de M1a.

## Amendement passe 6 (2026-11-03)
F6 (GAP-6-06).
- Pendant la bascule, le volume de téléchargement de Suwayomi (monté directement dans Kavita) est monté dans Shelfhold en **`adopt_ro`** (`:ro`, par exemple `/upstream/suwayomi`) : adoption (ComicInfo, hash, `plugin/parse`), test `(dev, ino)` et rapatriement lisent là et écrivent dans une racine `rw` de `/library` (EF-EXP-060). Le compose d'exemple et la doc de bascule montrent ce montage, retiré une fois le contrôle Kavita fait.
- Un tome adopté couvre une plage de chapitres (`unit_coverage`) : le filigrane est calculé en numéros de chapitre et une série adoptée en tomes seuls passe en `coverage_unknown` (EF-EXP-053, ADR-0040).

## Exigences liées
EF-EXP-003, EF-EXP-018, EF-EXP-028, EF-EXP-043, EF-EXP-044, EF-SRC-025, EF-SRC-026, EF-BIB-035, ENF-OPS-015, ENF-OPS-017, EF-EXP-045, EF-BIB-028, EF-EXP-051, EF-EXP-052, EF-EXP-053, EF-EXP-054, EF-EXP-055, EF-EXP-056, EF-EXP-060, EF-BIB-044.

## Sources
- https://wiki.kavitareader.com/guides/scanner/
- https://github.com/Suwayomi/Suwayomi-Server/issues/1004
- `deliverables/review/explorateur-passe3.md` (P1, P2, P4), `deliverables/review/ARBITRAGES-passe3.md` (C14)
- `deliverables/review/chasseur-trous-passe4.md` (GAP-4-08)
- `deliverables/review/ARBITRAGES-passe4.md`
- `deliverables/review/chasseur-trous-passe5.md`
- `deliverables/review/explorateur-passe5.md`
- `deliverables/review/ARBITRAGES-passe5.md`
