# Désinstaller Shelfhold

Deux niveaux : **détacher** (Shelfhold reste installé, vos scripts reprennent la main) et **désinstaller**
(retirer le conteneur). Aucun ne supprime vos livres. La seule étape destructrice est la dernière, et elle est
facultative.

## Avant tout : sauvegarde et kit de secours

1. **Sauvegarde** : `shelfhold backup run` puis `shelfhold backup verify` ([sauvegarde.md](sauvegarde.md)). Gardez
   l'archive **et** sa phrase de passe hors de la machine.
2. **Kit de secours** : le kit de secours imprimable (sans aucun secret) est **à venir** avec l'assistant de première
   installation. Tant qu'il n'est pas livré, notez vous-même où sont l'archive, la phrase de passe et la
   clé de l'instance.
3. Si vous voulez garder vos données lisibles hors Shelfhold : export ouvert (**à venir**).

## Étape 1 : rendre à Suwayomi ce qui lui appartient

`shelfhold detach --restore-upstream` : voir [retour-arriere.md](retour-arriere.md). Aucun fichier n'est supprimé.
Si des fichiers adoptés dépendent d'un volume de Suwayomi, rapatriez-les d'abord
([rapatrier-avant-demontage.md](rapatrier-avant-demontage.md)).

## Étape 2 : arrêter et retirer le conteneur

```sh
docker compose down
```

Cette commande retire les conteneurs, **pas** les dossiers `data` et `store`. Vos bibliothèques et Kavita sont
intacts.

## Ce qui reste, ce que vous décidez

| Dossier | Contient | Si vous le supprimez |
| --- | --- | --- |
| votre bibliothèque | les livres rangés | **ne le supprimez pas** : c'est votre collection |
| `.shelfhold/` dans chaque racine | état de Shelfhold | sans conséquence pour la lecture |
| `/data` | base, réglages, trousseau de secrets | l'historique, les liaisons et les réglages sont perdus ; la sauvegarde permet de les restaurer |
| `/store` | copies conservées des livres | **irréversible : ces copies peuvent être les seuls exemplaires** |

## Étape 3, facultative : effacer `/data` et `/store`

ATTENTION : `/store` peut contenir l'**unique** exemplaire d'un livre dont la source a disparu. Ne le supprimez que
si l'archive de sauvegarde est vérifiée et rangée ailleurs, et si vous avez relu le contenu de `/store`. Cette
étape ne se défait pas. Shelfhold ne la fait jamais pour vous.

```sh
# Relisez d'abord. Ne lancez rien tant que la sauvegarde n'est pas vérifiée.
ls -la /chemin/de/votre/store
```

Pour effacer, une fois sûr, supprimez vous-même les dossiers de votre choix avec les outils de votre plateforme. Cette
page ne donne volontairement aucune commande de suppression de `/store`.

## Retour

Pour revenir, réinstallez, puis restaurez la sauvegarde ([restauration.md](restauration.md)).
