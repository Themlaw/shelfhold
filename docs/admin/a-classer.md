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
administrateur : classer une Œuvre, classer une sélection en masse, voir le plan de retrait qu'un
relèvement produit (affiché, jamais appliqué). Les titres sont affichés comme du texte. Sans session,
la page répond « Connexion requise » : connectez-vous d'abord. Classer une Édition seule se fait par la
CLI.

### Classer en masse : une sélection, un aperçu, un seul travail

On ne classe plus « tout ce qui attend » depuis l'écran : on **choisit**.

1. Cochez les Œuvres (ou « toutes celles qui correspondent au filtre » : le filtre est alors figé en
   la liste de ce qu'il couvre à ce moment-là), choisissez l'âge, demandez l'**aperçu**. L'aperçu ne
   classe rien : il dit combien d'Œuvres seront classées, lesquelles, et combien d'identifiants ont été
   ignorés (déjà classés, inconnus : ils ne sont pas nommés). Un aperçu non appliqué est oublié après
   30 minutes.
2. **Appliquez** : un seul travail est mis en file (`library.bulk_rate`), visible dans la file. Il ne
   porte que l'identifiant de la commande. Il relit la sélection **sous verrou d'écriture** : si l'une
   des Œuvres choisies a changé depuis l'aperçu (classée entre-temps par quelqu'un d'autre), **rien**
   n'est classé et le travail finit en erreur `bulk.stale` ; refaites l'aperçu. Une Œuvre hors de la
   sélection n'est jamais touchée, même si la liste a changé. Une ligne d'audit par Œuvre, qui nomme la
   commande.
3. La commande appartient à celui qui a fait l'aperçu : celle d'un autre profil, un identifiant forgé et
   un aperçu expiré reçoivent la même réponse (404). Une commande ne s'applique qu'une fois (409 ensuite).

API : `POST /api/v1/bulk/rate/preview` (`{age, work_ids}` ou `{age, filter}`), puis
`POST /api/v1/bulk/rate/{order_id}/apply` (`{confirm}`), qui répond `202` avec le `job_id`. Si
l'administrateur perd le droit de classer entre la commande et le travail, le travail finit en erreur
`bulk.refused` et ne classe rien. Rejouer ou relancer ce travail (`jobs retry`, `jobs replay`) demande la
même session fraîche que l'ordre.

## Droits

Classer est une action réservée à l'administrateur : une clé d'API ou un jeton est refusé (403). Elle
exige un mot de passe (ou une passkey) saisi il y a moins de 900 s (`SHELFHOLD_FRESH_AUTH_S`), **dans tous
les profils** : sinon un 401 `reauth-required` ouvre la ré-authentification sur place, puis rejoue la même
action. Le classement en masse (aperçu et application) et `shelfhold rate` en ligne de commande suivent la même règle (la console de
l'hôte est exemptée). Voir
[roles.md](roles.md).

## Après un classement

- L'Œuvre devient admissible dans les étagères dont le plafond la couvre, et son suivi suspendu
  reprend.
- L'âge est poussé vers Kavita sur la série, verrouillé, au passage d'export suivant
  ([controle-parental.md](controle-parental.md)).
- Reclasser est aussi un classement : même commande.
