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

## Ce que chaque disque sait faire

Au démarrage, Shelfhold essaie sur chaque racine en écriture trois choses, avec des fichiers
d'essai qu'il supprime aussitôt : cloner un fichier (**reflink**, btrfs ou XFS récent), faire un
**lien dur vers `/store`** (possible seulement si la racine et `/store` sont sur le même système de
fichiers) et garder des **inodes stables** (un système de fichiers qui les renumérote fausse
l'identité des fichiers). `shelfhold status` affiche le résultat de chaque racine, avec le repli
« copie » quand ni l'un ni l'autre n'est possible (EF-EXP-015). `/store` ne peut pas partager le
système de fichiers de toutes les racines : avec un sous-disque par bibliothèque, au plus une
racine aura le lien dur. En M1a (mode passage), il n'y a pas de `/store` : le lien dur est
affiché « non applicable ». Rien d'autre n'est écrit sur la racine ; un redémarrage refait les
essais.

## Partages et sauvegardes de l'hôte

Chaque racine porte son **miroir de provenance** (`.shelfhold/manifest/`, EF-EXP-063) : la lignée
de chaque fichier exporté, qui sert à réadopter après une restauration ou une perte de `/data`.
Shelfhold le crée en `0700`/`0600` quand l'uid le permet. Quand l'uid est partagé avec Kavita, ces
droits sont impossibles et le miroir reste lisible par qui monte le dossier. Conséquences :

- un **partage réseau** (SMB, NFS) de `/library` expose aussi `.shelfhold/` (titres, équipes,
  identifiants distants, jamais d'âge ni de visibilité) : excluez ce dossier du partage, ou
  limitez-le aux comptes qui lisent déjà la bibliothèque ;
- la **sauvegarde de l'hôte** qui copie `/library` embarque le miroir et le journal de pierres
  tombales. Ils sont voulus : ils permettent de reconstruire la base. Conservez ces sauvegardes
  aussi sobrement que la bibliothèque, et sachez que la purge (`purge --now`) ne les atteint pas.

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
sorties `shelfhold root takeover`, `root reinit` et `root restored`. `takeover`
ré-estampille la sentinelle en gardant la liste des identifiants prédécesseurs ; `reinit` passe les
éléments en `missing` sans rien supprimer et ne réexporte rien ; `restored` replanifie ce qui manque par le
pipeline habituel (barrière parentale comprise). Aucune ne supprime un fichier ni ne touche à `.shelfhold/`
autrement qu'en y ajoutant ; `takeover` et `reinit` sont des actions réservées. La sentinelle ne recule jamais.
Seule `root_lost` se lève d'elle-même quand la sentinelle revient ; les autres attendent leur commande.
`<racine>` est l'identifiant que montre `shelfhold status`, ou le chemin absolu du dossier.

Le même principe vaut pour `/store` : `shelfhold store reinit` estampille `/store` et passe en
`store_missing` les chapitres dont la copie conservée y était. Après un déplacement de disques (rsync,
dates conservées), `shelfhold reconcile --rebaseline-inodes` remet à jour les numéros d'inode des fichiers
dont la taille, la date et l'empreinte concordent avec le miroir de provenance ; il refuse tout si une
empreinte diffère.

## Remarque sur NFS et disques durs

Les racines peuvent être en réseau ou sur disque dur. Le bail et l'écriture n'ont lieu que pendant
une session d'écriture, et les tâches périodiques lourdes sont regroupées dans la fenêtre de
maintenance (03:00 à 07:00 par défaut, `Europe/Paris` ; réglable par `SHELFHOLD_MAINTENANCE_WINDOW_START`, `_END` et `SHELFHOLD_TIMEZONE`), pour laisser les disques se mettre en veille.
