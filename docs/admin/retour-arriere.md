# Retour arrière : détacher Shelfhold

`detach` défait la bascule sans toucher à vos fichiers (EF-EXP-056).

## Ce qui est rendu, ce qui reste

- **Rendu à Suwayomi** : la catégorie d'exclusion que Shelfhold a posée et notée (la catégorie « Shelfhold ») est retirée ; celle que vous avez ajoutée vous-même reste en place et `detach` la liste. Sans note (installation d'avant les notes), seule une catégorie nommée « Shelfhold » est retirée, les
  catégories qu'il avait relevées avant d'agir sont remises si elles existent encore, et la
  stratégie de mise à jour automatique est restaurée. Shelfhold relève ces réglages **avant** de
  rien changer, au moment de l'appariement ; le premier relevé gagne.
- **Laissé en place** : tous les fichiers et dossiers (adoptés ou écrits par Shelfhold), la
  bibliothèque Kavita, ses exclusions (retirez `**/.shelfhold/**` à la main si vous le voulez), et
  l'historique de suivi. Les liaisons passent en inactives, en « manuel ».
- Le rapport liste les dossiers et fichiers que Shelfhold a nommés, pour que vous décidiez de leur sort.

Si le relevé manque ou est illisible (stratégie hors énumération, identifiants invalides), seule
l'exclusion est retirée et le rapport le signale ; aucune écriture douteuse n'est tentée. Une série
dont la restauration échoue garde son suivi et son relevé, et sera reprise à la commande suivante ;
les autres continuent.

## Procédure

1. Sauvegarde ([restauration.md](restauration.md)).
2. `shelfhold detach --restore-upstream [--shelf <id>|--series <id>]` (sans portée : tout ce qui est suivi
   par appariement). Suwayomi retrouve ses catégories et sa stratégie de mise à jour ; aucun fichier n'est
   supprimé, donc ce n'est pas une action réservée (le détachement qui supprime des fichiers en est une).
3. Réactivez vos scripts et montages d'avant si vous les aviez retirés
   ([migration-depuis-scripts.md](migration-depuis-scripts.md)). Si vous avez rapatrié des fichiers
   ([rapatrier-avant-demontage.md](rapatrier-avant-demontage.md)), ils sont désormais sous
   `/library` : remettez l'ancien montage en lecture seule seulement si vous tenez à l'ancien chemin.
4. Pour supprimer toute trace de Shelfhold, arrêtez l'instance et retirez les dossiers
   `.shelfhold/` de vos racines. C'est la seule chose qui reste sur votre bibliothèque.

Revenir à une version antérieure de l'image est autre chose : [redeploiement.md](redeploiement.md).
