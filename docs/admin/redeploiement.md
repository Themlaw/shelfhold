# Mise à jour et redéploiement

Recréer le conteneur ne perd rien : Shelfhold n'écrit jamais hors de ses volumes (`/data`,
`/store`, `/library`), son système de fichiers est en lecture seule et ses tâches sont reprises
au démarrage (ENF-OPS-017).

## Mettre à jour

1. Lisez la section de la version dans [`CHANGELOG.md`](../../CHANGELOG.md), notamment la ligne
   « Migration : oui/non, réversible ».
2. Sauvegardez `/data` ([restauration.md](restauration.md)).
3. Changez la version de l'image dans le compose et redéployez avec votre outil habituel
   (`docker compose pull && docker compose up -d`, ou un outil de gestion de piles).

Le service `shelfhold-migrate` tourne d'abord et le service principal en dépend
(`service_completed_successfully`). `shelfhold migrate` :

- refuse tant qu'une intention de fichier est ouverte (redémarrez l'ancienne version et laissez-la
  reprendre ou annuler l'intention) ;
- sauvegarde la base avant de migrer (`/data/backups/pre-migration`) et la remet si la migration échoue ;
- **refuse de démarrer sur un schéma plus récent** que celui de l'image : pas de retour en arrière
  automatique.

## Étiquettes d'image

Épinglez `X.Y` (`0.2`, puis `0.3`…) : vous recevez les correctifs (`0.2.x`) sans migration ; un
correctif n'en contient jamais. Il n'y a pas d'étiquette `latest`. `X.Y.Z` est immuable. Une mise à
jour d'une version mineure se lit dans le CHANGELOG avant d'être appliquée.

## Comportement au redéploiement

- Le conteneur reçoit SIGTERM et dispose de 60 s ([garde-disque.md](garde-disque.md)) pour finir
  proprement ; une tâche non finie reprend au démarrage.
- **Aucune planification sur l'hôte** : toutes les tâches périodiques vivent dans Shelfhold. N'ajoutez
  ni cron ni timer systemd pour lui.
- Un seul conteneur doit utiliser les volumes. Une seconde instance refuse d'écrire
  (`instance.lease_conflict`) ; pendant un redéploiement, arrêtez l'ancien avant de démarrer le nouveau
  (`docker compose up -d` le fait).
- Les commandes d'administration qui prennent le verrou (`secrets rotate|export`, `reconcile
  --adopt-after-restore`, `migrate`) refusent de tourner tant que l'instance tourne : arrêtez-la
  d'abord ([secrets.md](secrets.md)). Les autres passent par la socket quand elle tourne.

## Revenir à la version précédente

Voir « Retour à une image antérieure » dans [restauration.md](restauration.md).
