# Qui possède chaque champ : Shelfhold ou Kavita

Depuis Kavita 0.9.1, Kavita (et Kavita+) peut renommer une série, écrire des titres, des
identifiants externes et des couvertures, et dériver l'âge d'une série de ses tags. Shelfhold n'est
donc pas le seul à écrire. Cette page dit qui possède quoi, pour que personne n'écrase le travail
de l'autre.

## Règle

| Champ | Propriétaire | Ce que fait Shelfhold |
| --- | --- | --- |
| Fichiers, noms de dossiers et de fichiers | Shelfhold | Les écrit ; ne les réécrit jamais sans proposition validée |
| ComicInfo (dans l'archive) | Shelfhold | Le rend à l'écriture, dernier dans l'archive |
| Champs de son registre `field_owner` (dont l'**âge de série** des bibliothèques gérées) | Shelfhold | Les verrouille dans Kavita, les relit, les repousse s'ils dérivent |
| Tout autre champ **verrouillé** dans Kavita (titre, identifiants externes, couverture, résumé…) | Kavita (l'admin, Kavita+) | Ne le réécrit **jamais** |
| Nom de la série dans Kavita | Kavita | Le relit avant de conclure à une collision ; ne l'écrase pas |
| Âge dérivé des tags | Kavita | Ne s'en sert que comme complément : il peut relever le classement de Shelfhold, jamais l'abaisser |

Le registre `field_owner` note, pour chaque champ que Shelfhold a lui-même verrouillé, la dernière
valeur poussée et la date. Un verrou absent du registre est celui d'un tiers.

## Si Kavita montre une autre valeur pour un champ du registre

Shelfhold émet `reconcile.drift` (cause « champ modifié dans Kavita ») et repousse sa valeur, au
lieu de s'abstenir en silence. Pour qu'un champ reste à vous, ne le verrouillez pas dans Kavita
tant que Shelfhold le possède : changez-le dans Shelfhold.

## Recommandation

Sur les bibliothèques gérées par Shelfhold, verrouillez ou désactivez l'écriture de titre par
Kavita+ : une série renommée par Kavita+ n'est pas une collision, mais elle ne portera plus le nom
que Shelfhold a prévu.

## Après une réinstallation de Kavita

`shelfhold target reconnect` retrouve chaque bibliothèque par son dossier (aucune n'est recréée
en double), réapplique les réglages, oublie les identifiants de l'ancienne instance et **invalide
le registre `field_owner`** : les âges classés sont repoussés (à partir de M1b).
