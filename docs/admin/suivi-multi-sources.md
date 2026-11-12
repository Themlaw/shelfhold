# Suivi multi-sources : un rang, un repli, jamais de bascule silencieuse

Une série suivie peut avoir **plusieurs liaisons de source** : la même Édition publiée par deux sources. Le suivi porte
sur l'**Édition**, pas sur une liaison (EF-SRC-037). Cette page décrit ce que Shelfhold fait ; pour suivre une série
depuis Suwayomi, voir [suwayomi-multi-comptes.md](suwayomi-multi-comptes.md).

## Le rang

Chaque liaison active d'une Édition a un **rang** (`source_binding.priority`, 1 est le meilleur, unique parmi les liaisons
actives d'une même Édition). Pour une Unité nouvelle, Shelfhold choisit dans cet ordre :

1. les pierres tombales : une Unité purgée n'est pas redemandée ([purge.md](purge.md)) ;
2. les filtres de téléchargement (ci-dessous) ;
3. la préférence d'équipe, quelle que soit la liaison qui la publie ;
4. le **meilleur rang** qui a l'Unité.

Une Unité n'est **jamais demandée à deux liaisons dans le même cycle**.

## Le repli, toujours notifié

Une liaison devient inactive (`disabled_reason`) quand sa source n'est pas chargée trois cycles de suite, quand ses
erreurs de contenu atteignent le seuil, ou quand la série a quitté la bibliothèque amont. Ses demandes ouvertes sont
annulées et l'Unité est redemandée à la liaison suivante. Shelfhold émet `source.fallback` pour chaque transition :
**jamais de bascule silencieuse**. Quand la liaison d'origine revient saine, le retour au rang d'origine est
automatique tant que `SHELFHOLD_FOLLOW_FALLBACK_RETURN` vaut `true` (par défaut) ; il ne défait que le repli d'une source
non chargée, jamais ce que vous avez désactivé, ni les erreurs de contenu, ni ce qui a quitté la source. Une liaison
sans voisine dans son Édition reste active : il n'y a nulle part où se replier.

## Arrivée tardive d'un meilleur rang

Si une liaison de meilleur rang publie une Unité qu'une liaison de rang inférieur a déjà fournie, la politique
`series_follow.upgrade_policy` décide : `prefer_rank` remplace le fichier (mise à niveau au même chemin,
[mise-a-niveau.md](mise-a-niveau.md)), `keep_first` (par défaut) ne change rien. Par défaut, une Unité déjà lue garde son fichier
(« ne pas mettre à niveau les Unités lues »).

## Les filtres de téléchargement

Ils s'appliquent **avant** la demande, jamais après le téléchargement, et une Unité filtrée reste visible avec sa raison
(« filtrée : langue ») :

- **nouveautés seulement** (défaut) : le suivi ne demande rien au-dessous de son filigrane ;
- **langues** acceptées, **équipes** préférée, acceptées et refusées ;
- **exclure les extras** (numéros décimaux ou `part`, numéros hors du segment principal) ;
- **plafond par cycle** (50 par défaut) : l'excédent part en classe « rattrapage ».

Ces réglages se posent par étagère et par série. En 0.4, aucune commande de la CLI ne les écrit encore : l'écran qui les
règle arrive avec l'interface web (M3). Le comportement, lui, est actif.

## La cadence adaptative

Shelfhold n'interroge pas toutes les séries au même rythme : la prochaine vérification est calculée d'après la cadence
observée des chapitres (médiane des intervalles), bornée par `SHELFHOLD_FOLLOW_CHECK_MIN_S` (3600 s) et
`SHELFHOLD_FOLLOW_CHECK_MAX_S` (604800 s). Une série **terminée** ou **en pause** n'est vérifiée qu'une fois par
`SHELFHOLD_FOLLOW_CHECK_FINISHED_S` (2592000 s, 30 jours). Un contrôle immédiat d'une série :

```
shelfhold follow check <édition>
```

Il passe en classe « nouveautés » et ne contourne ni la gigue ni la limite par source ; une source déjà interrogée depuis
moins de `SHELFHOLD_FOLLOW_CHECK_MIN_S` n'est pas redemandée. En mode « toujours actif » de Suwayomi, Shelfhold ne
déclenche aucun rafraîchissement : la cadence ne sert qu'à dire quand une série est périmée (`series.stale`).

## Les trous de numérotation

Quand une série saute un numéro (le 12 puis le 14), Shelfhold le signale au lieu de le combler en silence. Si le
numéro n'existe pas, dites-le ; le trou n'est plus signalé :

```
shelfhold follow gap-ack <édition> <numéro> [--track main] [--segment <segment>]
shelfhold follow gap-ack <édition> <numéro> --undo     # reprend l'acquittement
shelfhold backfill <édition> --gaps [--dry-run]        # va chercher les numéros manquants
```

`backfill` demande une confirmation (`--confirm-units`, `--confirm-bytes`) au-delà de 100 chapitres ou 5 Gio.
