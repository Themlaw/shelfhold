# Sinistre sans sauvegarde : les racines qui ne sont plus les bonnes

Cette page couvre le cas où `/data` est perdu ou remplacé, ou un disque de la bibliothèque a été
changé, sans sauvegarde récente de la base. Shelfhold ne supprime **aucun** fichier dans ces cas :
il s'arrête d'écrire sur la racine concernée, le dit dans `shelfhold status`, et attend une
commande de votre part.

## Les quatre états

| État | Ce qui s'est passé | Commande | Réservée |
| --- | --- | --- | --- |
| `foreign_root` | la sentinelle `.shelfhold-mount-id` est celle d'une autre instance | `shelfhold root takeover <racine>` | oui |
| `root_lost` | la racine n'a plus de sentinelle (disque neuf, fichier retiré) | `shelfhold root reinit <racine>` | oui |
| `root_behind` | la racine est plus ancienne que la base (copie remise en place) | `shelfhold root restored <racine>` | non |
| `store_lost` | la sentinelle de `/store` manque, est étrangère ou en retard | `shelfhold store reinit` | oui |

`<racine>` est l'identifiant que montre `shelfhold status`, ou le chemin absolu d'un dossier que la
base ne connaît pas encore. Les commandes réservées se lancent depuis la CLI de l'hôte ou depuis
une session administrateur récente ; une clé d'API est refusée.

## Ce que fait chaque commande

- **`root takeover`** : estampille de nouveau la racine comme la nôtre. L'identifiant de racine
  (`root_id`) est celui de la base, jamais celui du fichier ; l'identité précédente rejoint
  `predecessor_instance_ids`, et les pierres tombales restent lisibles par le trousseau importé.
  Une sentinelle que le trousseau ne confirme pas ne peut ni imposer un `root_id` ni ajouter des
  prédécesseurs : ces revendications sont ignorées (et dites) ; si le journal de la racine contient
  des lignes qui ne s'ouvrent pas, la commande refuse et demande d'importer le trousseau
  (`shelfhold secrets import`) d'abord.
- **`root reinit`** : comme `takeover`, et les éléments exportés sur cette racine passent en
  `missing`. Rien n'est réexporté ni appliqué : `shelfhold missing resolve --reexport` (ou
  `--unfollow`) propose la suite.
- **`root restored`** : la sentinelle rattrape la base (jamais l'inverse), et ce que la vieille
  copie n'a pas est replanifié ; le prochain export le réécrit par le pipeline normal.
- **`store reinit`** : estampille `/store`, les chapitres dont la copie conservée y était passent
  en `store_missing` ; rien n'est retéléchargé par la commande.

## Garanties

- Aucune commande ne supprime un fichier, sur aucun chemin ; aucune ne supprime `.shelfhold/`.
- Chaque commande prend les verrous des exports et des cycles : lancée pendant l'un d'eux, elle
  refuse (`root.busy`) et ne change rien.
- Chaque sortie réservée écrit une ligne d'audit dans la transaction de la base.

## Après un déplacement de disques

`shelfhold reconcile --rebaseline-inodes` remet à jour les identités `(dev, ino)` des fichiers d'une
bibliothèque copiée sur d'autres disques (`rsync`, dates conservées), sans mettre aucune série en
pause. Il exige que le miroir de provenance confirme chaque fichier et refuse tout (sans rien
modifier) si l'empreinte d'un fichier diffère de celle du manifeste.
