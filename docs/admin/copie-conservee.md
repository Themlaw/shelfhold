# Copie conservée dans `/store`

Une étagère en mode **conserver une copie** (`retention_mode = keep`) garde, pour chaque Unité, le
fichier tel que reçu dans `/store`, en plus de celui que Kavita lit dans `/library`. Le mode
passage, par défaut, n'en garde aucune (EF-BIB-027).

## Une copie, jamais un lien

La copie conservée est une **copie indépendante** : un inode à elle, des octets relus et comparés
avant d'être mis en place. Sur un système de fichiers qui partage les blocs (btrfs, XFS avec
reflink), Shelfhold fait un clone (`FICLONE`) : l'espace n'est consommé qu'à la première
modification, mais le fichier reste un fichier à part. Ailleurs, il copie les octets et les vide
sur le disque. Il ne fait **jamais** de lien dur, même si `/library` et `/store` sont sur le même
volume.

## Ce que la page Santé compte

« Avec copie conservée » ne compte que les copies indépendantes :

| Cas | Compte ? |
| --- | --- |
| copie dans `/store`, inode distinct, empreinte sha256 relue identique | oui |
| clone reflink (inode distinct, blocs partagés en copie sur écriture) | oui |
| second nom du même inode (lien dur posé par un tiers) | non : un inode est une copie |
| fichier d'un autre contenu ou abîmé | non |

Le budget disque d'une Unité en copie conservée peut donc aller jusqu'à trois exemplaires
(Suwayomi, `/library`, `/store`) ; un clone reflink ne les consomme qu'en partie.

## Rattrapage après un changement de mode

Passer une étagère du mode passage à la copie conservée propose un rattrapage : la tâche
`store.keep_catchup` copie dans `/store` les Unités déjà rangées. Elle tourne dans la fenêtre de
maintenance (03 h-07 h), par lots de 200 Unités ou 5 Gio au plus, tant que la taille estimée tient
dans l'espace libre de `/store` moins la réserve de la garde disque. Le reste attend la nuit
suivante (événement `unit.safeguard_backlog`). La tâche s'arrête entre deux Unités quand la fenêtre
se ferme et reprend où elle s'est arrêtée ; une copie interrompue par un arrêt brutal est terminée
ou annulée au démarrage suivant, jamais à moitié mise en place.

Un fichier de `/library` que l'export a déjà réécrit (ComicInfo) n'est pas l'original : Shelfhold en
extrait les pages, vérifie l'empreinte du contenu et ne garde jamais un fichier rendu dans `/store`.

## `store/INDEX.jsonl`

Chaque nuit, dans la fenêtre, Shelfhold réécrit `INDEX.jsonl` à la racine de `/store` s'il a changé :
une ligne JSON par fichier conservé (`unit`, `path`, `title`, `lang`, `number`, `sha256`). Si `/data`
est perdu, `/store` reste lisible. Aucune Unité sous pierre tombale n'y figure ; il ne contient ni
secret ni chemin hors de `/store`, et il se régénère sans perte.
