# Désinstaller Shelfhold

Trois niveaux : **détacher** (Shelfhold reste installé, vos scripts reprennent la main), **détacher complètement** une
cible Kavita (Kavita fonctionne seul, sans trace de Shelfhold) et **désinstaller** (retirer le conteneur). Aucun ne
supprime vos livres. La seule étape destructrice est la dernière, et elle est facultative.

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

## Étape 1 bis : détacher complètement Kavita

Quand Kavita doit fonctionner **sans Shelfhold**, avec l'arbre de fichiers tel qu'il est :

```sh
shelfhold detach --full --target <identifiant-de-la-cible>
```

Le premier appel **ne change rien** : il affiche ce qui va se passer, puis la phrase à taper. Relancez la même
commande avec `--confirm "detach <identifiant>"` et, pour révoquer la clé, `--password-file <fichier>` (le mot de passe
du compte Kavita dédié, jamais en argument).

Dans cet ordre, et chaque pas peut être refait si une coupure l'interrompt (relancez la même commande) :

1. le **dossier réservé** `.shelfhold/` et la sentinelle de chaque racine sont effacés (une racine qu'une autre cible
   utilise encore est laissée telle quelle et signalée) ;
2. les **réglages de la bibliothèque Kavita** reviennent à ceux d'avant l'adoption : `ExcludePatterns` comme avant
   (le motif de Shelfhold part, les vôtres restent), type de bibliothèque relu et comparé, jamais forcé ;
3. la **clé dédiée est révoquée** dans Kavita ;
4. Shelfhold **oublie** son manifeste et la cible. **Aucun fichier exporté n'est touché** : mêmes octets, mêmes noms.

Un **rapport final** liste ce qui reste en place, à vous d'en décider : les téléchargements restés dans Suwayomi, les
comptes Kavita (le compte dédié, entre autres) et `/store`.

Ce qui l'arrête, sans rien changer :

- le dossier réservé contient des données à vous : un livre supprimé encore dans la **corbeille**, ou une **archive
  reçue qui attend son export**. Elles ne sont détruites qu'avec `--discard-hidden-data`, dit expressément ;
- une opération sur fichiers est encore ouverte sous la racine (relancez la commande une fois la reprise faite) ;
- la phrase n'est pas celle affichée.

Le journal d'effacement (pierres tombales) gardé dans `.shelfhold/` part avec lui : la base garde la référence. Si
Shelfhold suit encore des séries, lancez d'abord `shelfhold detach --restore-upstream` (étape 1).

Un détachement **interrompu** (coupure de courant, arrêt forcé) : relancez la même commande pour le finir, ou
`shelfhold detach --full --target <id> --give-up` pour l'abandonner proprement.

C'est une **action réservée** : une session fraîche de l'administrateur ou la ligne de commande de l'hôte, jamais une
clé d'API, un membre ou un enfant.

## Retirer une seule destination

```sh
shelfhold target remove <identifiant-de-la-destination> --keep-files     # garder les fichiers, oublier le manifeste
shelfhold target remove <identifiant-de-la-destination> --delete-files   # tout retirer
```

Même déroulement : un premier appel qui affiche l'avertissement et la phrase `remove <identifiant>`, puis
`--confirm`. Avec `--keep-files`, rien n'est écrit sur le disque. Avec `--delete-files`, **tout ce que Shelfhold a
écrit là est supprimé pour de bon, sans passer par la corbeille** ; les volumes que vous aviez déjà (adoptés) ne sont
jamais supprimés. Un fichier qui ne peut pas partir (modifié depuis son export, remplacé par un lien) est laissé et
listé, et la destination n'est pas oubliée tant qu'il en reste. Chaque suppression passe par une opération journalisée
que la même commande termine après une coupure (`--give-up` pour l'abandonner).

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
| `.shelfhold/` dans chaque racine | état de Shelfhold | sans conséquence pour la lecture ; `detach --full` l'efface (avec sa corbeille et ses archives en attente si vous le dites) |
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
