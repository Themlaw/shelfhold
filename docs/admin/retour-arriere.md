# Retour arrière : détacher Shelfhold, ou revenir à la 0.2.0

Deux retours différents : [détacher Shelfhold](#détacher-shelfhold) (vos scripts reprennent la main, Shelfhold
reste installé) et [revenir à la 0.2.0](#revenir-de-la-03-à-la-020) (l'image précédente).

## Détacher Shelfhold

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

## Revenir de la 0.3 à la 0.2.0

La 0.3 ajoute des migrations de base (0013 à 0017, voir le CHANGELOG) et **n'est pas réversible
automatiquement** : la 0.2.0 refuse de démarrer sur un schéma plus récent que le sien. Le retour passe par la
sauvegarde que `shelfhold migrate` a faite **avant** la migration, dans `/data/backups/pre-migration`. Ce que
vous avez fait avec la 0.3 depuis (classements, étagères famille, passkeys, comptes de service, purges) n'est
pas dans cette base ; les fichiers rangés depuis sont, eux, toujours là et ne sont jamais supprimés.

Le service qui fait la réconciliation doit être **le service complet, avec toutes les racines montées**
(`shelfhold` du compose, pas `shelfhold-migrate`, qui n'a pas `/library`). Une racine non montée n'est pas
jugée : sa sentinelle n'est pas relue, et le démarrage suivant retrouve la racine en retard
(`restored_behind`).

1. Arrêtez le service : `docker compose stop shelfhold`.
2. Remettez la base d'avant la migration, puis supprimez les fichiers du journal :
   `cp /data/backups/pre-migration/<fichier> /data/shelfhold.db`, puis retirez `shelfhold.db-wal` et
   `shelfhold.db-shm` (par un conteneur qui monte `/data`).
3. Changez l'étiquette de l'image pour `0.2` (ou `0.2.0`) dans votre compose.
4. Lancez la réconciliation avec le **service `shelfhold`** et toutes ses racines, hors ligne :

       docker compose run --rm --no-deps shelfhold shelfhold reconcile --adopt-after-restore --dry-run
       docker compose run --rm --no-deps shelfhold shelfhold reconcile --adopt-after-restore --confirm <empreinte>

   L'aperçu annonce les racines en retard et dit « Rien ne sera supprimé ». La 0.2.0 relit la sentinelle
   que la 0.3 a écrite (format `1.2`, avec la chaîne des pierres tombales) sans perdre un champ, et la
   réestampille. Les pierres tombales posées sous la 0.3 restent respectées.
5. Redémarrez : `docker compose up -d shelfhold`. `shelfhold status` ne doit plus lister `restored_behind`.

Les sessions et clés d'API ouvertes depuis la sauvegarde ne sont pas rétablies ; reconnectez-vous
([restauration.md](restauration.md), section « Retour à une image antérieure »).
