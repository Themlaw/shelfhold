# Œuvres liées

Une **relation** lie deux œuvres qui restent deux : un roman et son adaptation, une suite, une
préquelle, un dérivé. Elle ne fusionne rien : chaque œuvre garde ses éditions, ses unités et sa
classification d'âge (la fusion est une autre décision, réservée).

## Faire et retirer une relation

Réservé à l'administrateur (session, ou clé d'API de portée `admin`) :

- `POST /api/v1/works/{id}/relations` avec `{"to_work_id": "…", "kind": "sequel_of"}` : l'œuvre `{id}`
  **est** `kind` de `to_work_id`. Types : `adaptation_of`, `sequel_of`, `prequel_of`, `spin_off_of`.
- `DELETE /api/v1/works/{id}/relations/{kind}/{to_work_id}` la retire.
- Sur la fiche de série, le bloc « Œuvres liées » fait la même chose par une recherche de titre.

Une œuvre ne se lie pas à elle-même. Une relation qui fermerait une boucle du même type (une suite de sa
propre suite) est refusée (`relation.cycle`). Quand deux œuvres sont fusionnées, les relations de
l'absorbée passent à la survivante.

## Ce que voit un profil

`GET /api/v1/works/{id}/relations` donne les relations de l'œuvre, lues de son côté. Un profil ne voit
une relation que si l'**autre** œuvre lui est visible (plafond d'âge et, pour un enfant, une étagère qui
lui est ouverte) : ni le titre, ni le type, ni le compte d'une relation vers une œuvre cachée ne lui
parviennent, et l'œuvre cachée elle-même répond comme une œuvre qui n'existe pas.

## Kavita : séries liées

À chaque passe d'export, Shelfhold écrit les relations dans les « séries liées » de Kavita, avec la clé
du compte de service de la cible (jamais celle d'un membre) :

- **seulement entre deux séries dont les bibliothèques sont ouvertes aux mêmes profils** : même plafond
  d'âge et mêmes profils autorisés sur les étagères qui les portent. Une relation entre la
  bibliothèque d'un enfant et celle des adultes n'est jamais poussée, dans aucun sens ; si deux
  bibliothèques cessent d'avoir le même public, ce qui avait été poussé est retiré ;
- ce qu'une personne a ajouté à la main dans Kavita n'est pas touché : Shelfhold ne retire que ce qu'il
  a lui-même poussé ;
- rien n'est écrit tant que la destination est en pause.
