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

1. le **dossier réservé** `.shelfhold/` de chaque racine est effacé, puis la sentinelle en dernier (une racine qu'une
   autre cible utilise encore, ou sur laquelle vit la bibliothèque de Shelfhold, est laissée telle quelle et signalée) ;
2. les **réglages de la bibliothèque Kavita** reviennent à ceux d'avant l'adoption : le type de bibliothèque est relu
   et comparé, jamais forcé ; une bibliothèque supprimée dans Kavita entre-temps est signalée (`library_gone`) et ne
   bloque rien. **Écart assumé avec la lettre de la spécification** : `ExcludePatterns` devient *les motifs actuels
   moins celui de Shelfhold* (sauf s'il y était déjà avant l'adoption), et non la liste d'avant l'adoption recopiée
   telle quelle ; un motif que vous avez ajouté depuis reste, un motif que vous avez retiré depuis n'est pas remis.
   Quand plusieurs destinations ont été adoptées sur la même bibliothèque, les motifs d'avant de chacune sont
   respectés. **Le motif qui cache `.shelfhold/` reste** sur toute bibliothèque dont un dossier contient un
   `.shelfhold/` laissé en place (racine utilisée par une autre cible, ou bibliothèque de Shelfhold) : sans lui,
   Kavita indexerait la corbeille et les archives reçues. Le rapport le dit (`hidden_pattern_kept`) ;
3. la **clé dédiée est révoquée** dans Kavita **par son identifiant**, noté dans le journal avant la révocation (jamais
   son nom : une autre clé du même compte n'est jamais touchée). Si une coupure survient juste après, la même commande
   termine : une clé absente dont l'identifiant est noté compte comme révoquée, et un identifiant que Kavita a
   entre-temps donné à une autre clé (valeur différente de celle notée, par somme de contrôle) est laissé en place et signalé
   (`key_id_reused`) ;
4. Shelfhold **oublie** son manifeste et la cible. **Aucun fichier exporté n'est touché** : mêmes octets, mêmes noms.

Un **rapport final** liste ce qui reste en place, à vous d'en décider : les téléchargements restés dans Suwayomi, les
comptes Kavita (le compte dédié, entre autres) et `/store`.

Ce qui l'arrête, sans rien changer :

- le dossier réservé contient des données à vous : un livre supprimé encore dans la **corbeille**, ou une **archive
  reçue qui attend son export**. Elles ne sont détruites qu'avec `--discard-hidden-data`, dit expressément ;
- le **journal d'effacement** (pierres tombales) gardé dans `.shelfhold/` partirait avec lui : c'est ce qui empêche une
  sauvegarde restaurée de ressusciter des Œuvres effacées. Il ne part qu'avec `--discard-tombstone-journal`, dit
  expressément, et le rapport final le dit ;
- une opération sur fichiers est encore ouverte sous la racine (relancez la commande une fois la reprise faite) ;
- un export, un scan ou une autre tâche tient une destination de la cible (`busy` : réessayez après) ;
- la phrase n'est pas celle affichée.

Si Shelfhold suit encore des séries, lancez d'abord `shelfhold detach --restore-upstream` (étape 1).

**La clé dédiée n'existe plus** (supprimée à la main dans Kavita, réponse 401) : sans option, le détachement ne
finit pas et le message propose `--key-gone`. Avec `--key-gone --password-file <fichier>`, la révocation est sautée
(le rapport dit `key_gone`) et les réglages de la bibliothèque sont rendus par une connexion du compte administrateur
au lieu de la clé morte. Si le compte voit encore la clé, elle est révoquée comme d'habitude : l'option ne laisse
jamais une clé vivante. Sans mot de passe, l'option est refusée.

Un détachement **interrompu** (coupure de courant, arrêt forcé) : relancez la même commande pour le finir. Avant que
l'effacement du dossier réservé commence, `shelfhold detach --full --target <id> --give-up` l'abandonne proprement et
dit combien d'opérations il annule. **Une fois l'effacement commencé, `--give-up` est refusé** (il n'y a rien à
défaire) : relancez la même commande pour terminer.

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
que la même commande termine après une coupure (`--give-up` pour l'abandonner) ; le fichier est relu juste avant
d'être effacé, aussi à la reprise : s'il a changé entre-temps, il reste. Une ligne du manifeste sans empreinte (`no_hash`)
n'est jamais supprimée, seulement listée. Les dossiers de séries que la suppression a vidés sont retirés ; un dossier
où il reste autre chose, et le dossier de la destination, restent. Un export en cours sur la destination fait répondre
« occupée » (`busy`), et inversement.

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
