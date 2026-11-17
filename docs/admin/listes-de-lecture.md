# Collections et listes de lecture

Une **collection** est un ensemble nommé et ordonné d'œuvres que l'administrateur fait pour toute la
maison. Une **liste de lecture** est la même chose, tenue par un profil : elle est **privée par
défaut**, lue par son auteur et par l'administrateur, et par personne d'autre.

## Qui voit quoi

- Un profil lit les collections de la maison et ses propres listes. La liste d'un autre profil
  n'existe pas pour lui : même réponse qu'un identifiant que personne n'a jamais eu.
- Un enfant ne voit d'une liste que les œuvres qu'il peut voir ailleurs : sous son plafond d'âge, et
  rangées sur une étagère qui lui est ouverte (`allowed_profiles`). Le compte, l'ordre (numéroté de 1,
  sans trou), la couverture et le nom ne viennent que de ces œuvres.
- Le **nom** d'une collection de la maison qui contient une œuvre cachée à l'enfant lui est retenu :
  un nom comme « … tome 9 » pourrait en dévoiler une. Une collection qui ne contient que des œuvres
  cachées n'existe pas pour lui.
- Seul l'administrateur fait ou modifie une collection de la maison. Une clé d'API lit comme le profil
  auquel elle appartient et n'écrit rien ici.

## Importer un fichier CBL

`POST /api/v1/collections/import-cbl` lit un fichier de liste de lecture au format CBL (ComicRack).
L'**ordre du fichier est gardé**. Chaque ligne est rapprochée d'une œuvre par son **titre**
(majuscules, espaces et formes larges ignorés) parmi les œuvres que le lecteur peut voir. Une ligne
qui ne correspond à rien, ou à plusieurs œuvres, est **listée** dans la réponse (`unresolved`, avec sa
raison `not_found` ou `ambiguous`) et laissée de côté : rien n'est deviné.

Le fichier est traité comme hostile : aucune déclaration de type de document (donc ni entité externe ni
expansion), 2 Mo, 8 niveaux, 5 000 livres au plus. Un fichier hors limite est refusé en entier
(422 `collection.cbl_refused`) et ne crée rien.

La poussée des collections vers Kavita arrive avec L18b.
