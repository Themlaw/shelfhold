# Classer les œuvres : « À classer » et « À ranger »

Un classement est l'âge que vous donnez à une Œuvre (ou à une seule Édition). Shelfhold ne classe
jamais à votre place : un âge lu dans un fichier (ComicInfo), une sous-source ou un fournisseur de
métadonnées est une **suggestion**, visible mais sans effet sur ce qui entre dans une étagère famille.

Les âges possibles : `0`, `10+`, `12+`, `13+`, `16+`, `18+`, `X`.

## Ce qui attend un classement

`shelfhold status` liste, et l'écran « À classer » affiche :

- `rating_pending` : une Œuvre qui a une Édition non classée, retenue hors étagère ou placée sur une
  étagère famille. L'état n'apparaît que si une étagère famille existe : sur une installation sans
  étagère famille, « non classé » est l'état normal ;
- `follow_suspended` : une Œuvre adoptée dont le suivi est suspendu jusqu'à son classement ;
- les Œuvres **déjà classées** dont une Édition porte une suggestion plus élevée (état
  `suggestion_above`) : le classement de l'Œuvre ne règle jamais cette suggestion, il faut classer
  l'Édition.

La vue « À ranger » liste les Éditions sans étagère. Elle est en lecture seule : choisir l'étagère
d'une Édition passe par `POST /api/v1/editions/{id}/shelf`.

## En ligne de commande

    shelfhold rate 12+ --work <identifiant>
    shelfhold rate 18+ --edition <identifiant>
    shelfhold rate 12+ --pending

- `--work` classe une Œuvre, `--edition` une seule Édition (une adaptation jeunesse d'une Œuvre adulte,
  par exemple).
- `--pending` classe d'un coup toutes les Œuvres de « À classer » : lancée sans `--confirm`, la commande
  affiche la liste et un jeton ; relancez avec `--confirm <jeton>`. Le jeton est recalculé au moment
  d'écrire : si la liste a bougé entre-temps, rien n'est appliqué. Une entrée d'audit est écrite par
  Œuvre. Les Œuvres qui n'attendent qu'une suggestion d'Édition ne sont jamais classées par le lot.
- Un classement qui **relève** l'âge au-dessus du plafond d'une étagère famille n'applique **rien** : il
  affiche le plan de retrait, et c'est `shelfhold shelf withdraw` qui le lance
  ([famille.md](famille.md)).

## Écran

L'écran « À classer » de l'interface (`/a-classer`) fait la même chose avec une session
administrateur : classer une Œuvre, classer par lot, voir le plan de retrait qu'un relèvement
produit (affiché, jamais appliqué). Les titres sont affichés comme du texte. Sans session, la page
répond « Connexion requise » : connectez-vous d'abord. Classer une Édition seule se fait par la CLI.

## Droits

Classer est une action réservée à l'administrateur : une clé d'API ou un jeton est refusé (403). Elle
n'exige **pas** de mot de passe récent, sauf en profil durci ([durcir.md](durcir.md)), où un 401
`reauth-required` ouvre la ré-authentification sur place, puis rejoue la même action. Voir
[roles.md](roles.md).

## Après un classement

- L'Œuvre devient admissible dans les étagères dont le plafond la couvre, et son suivi suspendu
  reprend.
- L'âge est poussé vers Kavita sur la série, verrouillé, au passage d'export suivant
  ([controle-parental.md](controle-parental.md)).
- Reclasser est aussi un classement : même commande.
