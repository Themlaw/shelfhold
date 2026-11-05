# Plusieurs disques sous `/library` : les racines physiques

Si `/library/manga` et `/library/comics` sont deux disques différents (ou si un seul sous-dossier
est un autre disque), Shelfhold les traite comme des **racines physiques** distinctes
(EF-EXP-059). Il les reconnaît tout seul, par `st_dev`, à partir de la correspondance de chemins
de la destination ([headless.md](headless.md), clé `path_map.<cible>`).

## Ce que chaque racine porte

| Élément | Rôle |
| --- | --- |
| `.shelfhold-mount-id` | sentinelle : identifiant d'instance, de racine, compteur de génération, époque de restauration |
| `.shelfhold/staging` | écritures atomiques : sur le **même disque** que la destination |
| `.shelfhold/recycle` | corbeille de cette racine |
| `.shelfhold/lease` | bail d'écriture, pris seulement pendant une session d'écriture |
| `.shelfhold/manifest`, `.shelfhold/tombstones` | miroir de provenance et journal de pierres tombales |

Ces dossiers sont réservés à Shelfhold. Excluez `**/.shelfhold/**` de Kavita (fait à l'adoption,
voir [adopter-kavita.md](adopter-kavita.md)) et de vos outils de dédoublonnage.

## Disque non monté : jamais pris pour une bibliothèque vide

Une racine dont la sentinelle manque ou diffère (`mount.sentinel_missing`) **bloque les écritures,
suppressions et scans vers elle seule** ; les autres racines continuent. Un dossier vide qui
reste à la place d'un disque démonté n'est jamais interprété comme « tout a été supprimé ».
Remontez le bon disque, puis `shelfhold target recheck <cible>`.

Un déplacement d'un chapitre entre deux racines n'est jamais un `rename` : c'est une copie
vérifiée par empreinte, avec intention journalisée, puis suppression de l'ancienne.

## Racine remplacée, restaurée ou venue d'ailleurs

Une sentinelle plus récente que la base : [restauration.md](restauration.md) (`restored_behind`).
Une sentinelle d'une **autre instance** (`foreign_root`), un disque remplacé par un disque vide
(`root_lost`) ou une racine restaurée par l'hôte, plus vieille que la base (`root_behind`) :
sorties `shelfhold root takeover`, `root reinit` et `root restored` **[câblage, M1b]**. `takeover`
ré-estampille la sentinelle en gardant la liste des identifiants prédécesseurs ; `reinit` passe les
éléments en `missing` sans rien supprimer ; aucune ne touche à `.shelfhold/`. Ce sont des actions
réservées. En M1a, un tel état se lit dans `shelfhold status` et l'écriture vers la racine reste
bloquée.

Le même principe vaut pour `/store` (`shelfhold store reinit`, M1b).

## Remarque sur NFS et disques durs

Les racines peuvent être en réseau ou sur disque dur. Le bail et l'écriture n'ont lieu que pendant
une session d'écriture, et les tâches périodiques lourdes sont regroupées dans la fenêtre de
maintenance (03:00 à 07:00 par défaut, fixe en M1a), pour laisser les disques se mettre en veille.
