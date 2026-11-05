# Effacer pour de bon : `purge --now` et pierres tombales

Supprimer un titre de la bibliothèque (corbeille, suppression ordinaire) laisse la possibilité de
le retrouver. **Purger** est l'inverse : toutes les copies que Shelfhold gère disparaissent, et
**rien ne revient** ensuite, ni depuis Suwayomi, ni depuis une restauration (EF-BIB-052, EF-BIB-053).

## Lancer une purge

`shelfhold purge --now --unit <id> --confirm <id>` (ou `--work <id>`), et les routes d'administration
`POST /api/v1/units/{id}/purge` et `POST /api/v1/works/{id}/purge`.

- Action **réservée** : administrateur, mot de passe saisi il y a moins de 15 minutes, ou ligne de
  commande locale. Une clé d'API est refusée.
- **Confirmation tapée** : l'identifiant de l'Unité ou de l'Œuvre, toujours, même pour du contenu
  remplaçable. Le refus a lieu avant toute écriture, pierre tombale comprise.
- Une ligne d'audit est écrite, sans titre.

## Ce qui est supprimé

Toutes les copies gérées connues : les fichiers des destinations (sous `/library`), la copie
conservée de `/store` et les copies de la corbeille, les vignettes et caches dérivés, les lignes
de base qui les nomment (variantes, contenus, copies, exports, positions de lecture), et la
revendication côté Shelfhold des téléchargements de Suwayomi (les tickets sont clos).

Ce qui reste, volontairement :

- Le dossier de série vidé, et les lignes d'Œuvre et d'Édition (titres compris) jusqu'à M1b.
- Un fichier qui porte d'autres Unités (volume, archive multi-chapitres) n'est jamais effacé pour
  une seule : la purge s'arrête (`PurgeCollateralError`) et le rapport le dit.
- Une copie sur une racine en lecture seule (`adopt_ro`) n'est jamais touchée.

## Ce que Shelfhold ne peut pas effacer

Chaque rapport de purge le rappelle : les sauvegardes de l'hôte, les caches et signets de Kavita,
les copies faites hors de Shelfhold, les journaux de l'hôte, les sauvegardes retenues. Dans
Suwayomi, le chapitre n'est pas supprimé par Shelfhold : l'alerte `unit.delete_in_suwayomi` vous
demande de le faire.

## Les pierres tombales

Avant de supprimer quoi que ce soit, la purge écrit une **pierre tombale** : l'enregistrement de
ce qui ne doit pas revenir. Elle est dans la base **et** dans un journal par racine
(`.shelfhold/tombstones`), en ajout seul, chiffré par la clé du trousseau. Elle ne contient aucune
empreinte en clair. Elle vise une variante précise, une place (série et numéro), la liaison d'une
série à sa source ou une Œuvre entière.

Conséquences :

- Un chapitre purgé n'est pas retéléchargé par le suivi, une série purgée n'est pas resuivie
  depuis la bibliothèque amont, un fichier dont l'empreinte est une pierre est refusé à la réception
  et à l'adoption.
- Une restauration de sauvegarde ne ressuscite pas une purge : le journal des racines est rejoué
  **avant** toute réadoption ([restauration.md](restauration.md)).
- **Racine absente** : la purge fait ce qu'elle peut ; les lignes de journal dues à la racine
  démontée sont gardées en base, et toute écriture vers cette racine est refusée tant qu'elles ne
  sont pas écrites. Relancez la même commande au remontage : elle termine.
- **Le trousseau compte** : sans la clé d'une pierre, Shelfhold ne peut plus dire ce qu'elle
  interdit ; il bloque alors la réacquisition de son périmètre. Gardez l'export de clé
  ([restauration.md](restauration.md)).

Lister ou lever une pierre tombale sont aussi des actions réservées ; aucune commande ne les
expose dans cette version.
