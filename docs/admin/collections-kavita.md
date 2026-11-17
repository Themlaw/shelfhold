# Collections et listes de lecture dans Kavita

Les collections du foyer et les listes de lecture de Shelfhold sont poussées dans Kavita à chaque
passe d'export (et toutes les 30 minutes, et tout de suite quand une étagère ou une liste change).

## Ce qui est poussé

- **Sous le compte de service.** Tout est créé avec la clé du compte dédié de la cible, jamais avec la
  clé d'un membre, puis **promu** : les comptes qui peuvent lire les séries le voient.
- **Une collection du foyer** n'est poussée que si toutes ses séries sont dans des bibliothèques ouvertes
  aux **mêmes profils** (même plafond d'âge, mêmes profils autorisés sur les étagères qui les portent).
  Une collection qui mélange les bibliothèques d'un enfant et celles des adultes n'est poussée nulle
  part, pas même amputée : son nom pourrait dévoiler un titre. Si deux bibliothèques cessent d'avoir le
  même public, ce qui avait été poussé est retiré.
- **La liste d'un profil** porte son nom (`profil — nom de la liste`). Elle reste sous le compte de
  service. Elle n'est **poussée dans Kavita qu'une fois approuvée**, puis promue seulement si
  toutes ses séries sont dans des bibliothèques ouvertes à ce profil **et** de la même audience.
  L'approbation (`PUT /api/v1/collections/{id}/kavita-public` avec `{"approved": true}`) demande une
  **session administrateur fraîche** (jamais une clé d'API) ; la retirer (`DELETE` sur la même
  route) reste possible à l'administrateur. Un élément ajouté ensuite retire l'approbation.
- Une série que Kavita n'a pas encore indexée est laissée de côté et ajoutée quand elle l'est ; une
  collection sans aucune série connue de Kavita n'est pas créée vide.

## Ce qui n'est jamais touché

- Ce qu'une personne a posé à la main dans Kavita : Shelfhold ne retire que les séries qu'il a ajoutées.
  Une collection de Shelfhold qui contient autre chose reste en place, non promue, quand Shelfhold la
  retire ; elle n'est supprimée que si elle est vide.
- Un objet du compte de service qui porte déjà le titre voulu et que Shelfhold n'a pas fait : rien n'est
  poussé sous ce titre (renommez l'un des deux).

## Pause et retrait

Un changement d'étagère ou de plafond retire ce qui est devenu interdit : d'abord la promotion, puis
les séries, puis l'objet s'il est vide. Ce retrait passe **même quand la destination est en pause** ;
créer, ajouter et promouvoir attendent la fin de la pause.

## Limites

- Une liste de lecture reprend les séries dans l'ordre de Shelfhold à sa création ; un changement
  d'ordre ensuite n'est pas propagé (les ajouts vont à la fin).
- Ce que Shelfhold sait de Kavita vient de sa base : si quelqu'un supprime à la main un élément poussé,
  il n'est recréé qu'au prochain changement de la collection.
- Après une restauration de sauvegarde, la mémoire de ce qui a été poussé est vide : à la passe
  suivante, un objet promu qui ne doit plus l'être est dépromu (jamais vidé ni supprimé), et un objet
  qui reste voulu reste non géré ; renommez ou supprimez-le à la main pour qu'il soit repris.
- Le détachement complet retire d'abord les collections et listes que Shelfhold a posées (au mieux,
  ce qui n'est pas atteint reste dans Kavita).
