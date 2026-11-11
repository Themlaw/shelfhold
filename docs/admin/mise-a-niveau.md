# Mise à niveau d'un chapitre au même emplacement

Quand une meilleure version d'un chapitre déjà exporté arrive, Shelfhold peut **remplacer le
fichier à son emplacement** (EF-EXP-016). Trois raisons déclenchent cette mise à niveau :

- une meilleure version (`better_version`) ;
- la version de l'équipe préférée (`preferred_team`) ;
- une réacquisition après un échec d'intégrité (`integrity_reacquire`).

Une révision enregistrée avant M2 n'en fait pas partie : elle n'est **jamais appliquée d'office**.
La file des révisions en attente s'en occupe (EF-BIB-043).

## Ce qui ne change pas : Kavita

Le nom du fichier, son dossier et sa ligne dans le manifeste restent les mêmes. Kavita voit le même
chapitre de la même série au même endroit : **la progression Kavita est conservée**.

## Ce qui change : KOReader

KOReader reconnaît un document à une empreinte de son contenu (MD5 partiel), pas à son nom. Un
nouveau fichier au même chemin est donc pour lui **un nouveau document** : la position de lecture et
les statistiques repartent du début. Pour cette raison, chaque mise à niveau :

- émet l'événement `unit.upgraded` avec `koreader_reset = true`, notifiable par les canaux
  configurés ;
- apparaît dans `shelfhold status`, sur une ligne « position KOReader réinitialisée », pendant
  7 jours.

L'écran Activité viendra en M3. L'empreinte de l'ancien fichier reste rattachée au chapitre
(`document_alias`), si bien qu'une position prise sur l'ancien fichier se retrouve encore.

## « Ne pas mettre à niveau les Unités déjà lues »

L'option s'active par étagère et vaut **oui** par défaut. Un chapitre qu'un lecteur a terminé n'est
alors jamais remplacé, quelle que soit la raison. Toutes les demandes de mise à niveau passent par
cette vérification. Pour la changer :

```
POST /api/v1/shelves/{shelf_id}/skip-read-upgrades   {"skip_read_upgrades": false}
```

La remise en place d'une ancienne version (plus bas) est demandée par une personne, pas par
Shelfhold : l'option ne s'y applique pas.

## Contenu irremplaçable

Si plus aucune source ne peut redonner le chapitre, il est irremplaçable (EF-BIB-031). Il n'est alors
remplacé que si son identifiant est **tapé** en confirmation. Une mise à niveau automatique ne peut
pas taper : elle s'abstient.

## Racine gelée

Si la racine qui porte le fichier est gelée (`foreign_root`, `root_lost`, `root_behind`,
`store_lost`, ou toute l'instance en `restored_behind`), rien n'est écrit, pas même l'intention.
La mise à niveau attend la sortie de l'état, voir [etats-attente-et-sorties.md](etats-attente-et-sorties.md).

## L'ancienne version va dans la corbeille

L'ancien fichier part dans la corbeille de sa racine, sous
`.shelfhold/recycle/<intention>/<chemin du fichier>`, où il reste 30 jours (ENF-OPS-009). Une purge
l'atteint comme toute autre copie. Pour le remettre en place, appelez
`SamePathUpgrade.restore(<upgrade_id>)` : la version servie part à son tour dans la corbeille.

## Coupure en plein remplacement

Le remplacement se déroule sous une intention `upgrade` du journal (EF-EXP-041) :

1. L'intention est enregistrée.
2. La nouvelle version est préparée dans `.shelfhold/upgrade/<intention>`.
3. Elle est échangée avec le fichier servi en une seule opération (`RENAME_EXCHANGE`).
4. L'ancien fichier rejoint la corbeille.
5. Le manifeste est mis à jour dans la transaction qui clôt l'intention.

Si le système de fichiers ne sait pas échanger deux noms, l'ancien fichier est d'abord copié dans la
corbeille, puis la nouvelle version le remplace par un `rename`.

Quel que soit l'instant de la coupure, **l'emplacement porte toujours un fichier, et un seul**, et
l'ancienne version n'est jamais perdue. Au redémarrage, l'intention est terminée ou abandonnée selon
ce que montre le disque. Un état inconnu est signalé (`reconcile.drift`) et rien n'est touché.
