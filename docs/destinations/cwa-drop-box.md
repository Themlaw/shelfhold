# Boîte de dépôt CWA

Une destination de type `cwa` (EF-EXP-022) dépose un **EPUB** dans le dossier d'ingestion de
Calibre-Web Automated. Shelfhold n'appelle aucune API : il écrit un fichier, et l'importeur de CWA
le prend, l'importe, puis le **supprime**. Le code est dans `backend/src/shelfhold/export/targets/cwa/`
(la destination décrit, la source lit la base, l'exécuteur de plans du cœur écrit).

## Ce qu'elle déclare

| Capacité | Valeur | Pourquoi |
|---|---|---|
| `transport` | `dropbox` | un dossier que quelqu'un d'autre vide |
| `accepted_content_types` | `["epub"]` | un seul format ; une série sans EPUB n'est pas déposée (EF-EXP-046) |
| `access_partitioning` | `none` | CWA ne sépare aucun compte : aucune étagère sous 18+ n'y est liée (EF-EXP-047) |
| `reconcilable`, `confirms_arrival`, `indexes_tree` | faux | pas de sentinelle, pas de détection d'un autre écrivain, pas d'arbre (EF-EXP-058) |
| `can_update`, `can_delete`, `can_purge` | faux | un dépôt ne se reprend pas |

## Comment un dépôt se passe

1. **Un fichier par livre**, à la racine du dossier d'ingestion, nommé `<titre> [<identifiant>].epub`
   (le titre est nettoyé en un seul composant de nom ; l'identifiant rend le nom unique).
2. **Écriture à côté, puis `rename`** : l'exécuteur écrit dans la zone temporaire de la racine, relit
   le fichier, puis le renomme à sa place. L'importeur ne voit jamais un fichier partiel ni un nom
   temporaire. Une coupure (kill -9) entre l'écriture et le `rename` ne laisse rien de visible ;
   l'intention du journal est réglée au démarrage.
3. **Suivi par identifiant** : le fichier disparaît après l'importation, il n'y a donc pas de chemin à
   surveiller. La ligne du manifeste est typée `idempotency_key` (une clé propre à ce dépôt, gardée
   d'une passe à l'autre) ; elle reste `written`, jamais `missing`. Un livre qui a une ligne n'est
   plus décrit.
4. **Coupure après le `rename` et avant la ligne** : l'intention `done` du journal dit que le livre a
   été déposé ; la passe suivante ne le redépose pas et remet la ligne du manifeste.

## Ce qui est refusé

- Une étagère sous 18+ : à la **liaison** (`shelf.destination_not_partitioned`) et à **chaque passe**
  (`refused`, rien n'est déposé). Aucune attestation de l'administrateur n'y change rien :
  `effective_partitioning("cwa", …)` rend `none`.
- Un transport autre que `dropbox` (`target.transport_not_supported`).
- Un sous-dossier qui remonte (`..`), absolu, ou qui sort du dossier de la cible par un lien
  (`target.folder_outside_root`), à la liaison **et** à chaque dépôt : l'exécuteur relit la ligne
  de la base, refuse tout lien sur le chemin et un dossier absent (il n'en crée pas).
- Un plan qui nomme un sous-dossier, un nom caché, un séparateur ou un autre format que l'EPUB, ou
  qui cite un livre d'une autre étagère.
