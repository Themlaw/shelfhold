# Contrôle parental : livres EPUB dans Kavita

La barrière, c'est la **bibliothèque** Kavita : une étagère famille (plafond sous 18+) écrit dans
une bibliothèque qui lui est réservée, et seuls les comptes qui y ont accès la voient. L'âge d'une
série ne sert qu'à trier, à l'intérieur de cette bibliothèque, ce que voit un compte enfant
restreint.

## Aucun âge dans le fichier

Un EPUB exporté ne porte **aucun âge** : ni `dc:subject` d'âge, ni métadonnée propre à Shelfhold.
Kavita ne lit pas d'âge dans un EPUB, et tout `dc:subject` y devient un genre. L'âge vit dans la
base de Shelfhold et passe par l'**API Kavita**, sur la série, **verrouillé**. Le verrou est inscrit
au registre `field_owner` (voir [proprietaire-des-champs.md](proprietaire-des-champs.md)).

Sans cet âge, Kavita classerait la série « inconnue » et la cacherait aux comptes enfants
restreints qui n'incluent pas les inconnus (ce que l'audit d'accès exige).

## L'ordre de l'export

Kavita ne crée une série qu'à partir d'un fichier. Pour une série de livres dont l'Œuvre (ou
l'Édition) est classée, l'export suit donc cet ordre :

1. Avant tout plan, Shelfhold interroge Kavita. Il faut qu'il réponde, en version **0.9.1 ou
   plus** (plancher ENF-COMP-001). Sinon, aucun livre n'est exporté.
2. Le **premier** livre de la série part seul. Kavita le range sous un âge « inconnu » : un compte
   enfant restreint ne le voit pas encore.
3. Après le scan, l'âge de la série est poussé, verrouillé, puis **relu**. La série n'accepte
   d'autres livres qu'une fois l'âge relu verrouillé, à la valeur voulue.
4. Si Kavita ne répond pas, refuse la poussée ou perd le verrou, les autres livres attendent
   (`export.awaiting_series_age`). Rien n'est perdu : l'export reprend au passage suivant.

Une Œuvre que personne n'a classée n'a pas d'âge à pousser. Une étagère famille la refuse de
toute façon ; une étagère adulte l'exporte sans attendre.

## Reclasser

Chaque reclassement repousse l'âge, verrouillé, **au début** du passage d'export suivant, avant
toute écriture. Une série reclassée vers le haut ne reçoit aucun nouveau livre tant que le nouvel
âge n'a pas été relu verrouillé. Si le reclassement dépasse le plafond de l'étagère, ce n'est plus
une poussée d'âge : c'est le plan de retrait (`shelfhold shelf withdraw`, état d'attente
`withdrawal_pending`), qui sort la série de la bibliothèque des enfants.

## Omnibus et autres reconditionnements

Une Édition `omnibus` (ou `kanzenban`, `deluxe`) de la même Œuvre est une **série Kavita
distincte**, par exemple « Le Livre (fr, omnibus) », avec son propre dossier et **son propre
âge** : celui qu'un admin a fixé pour cette Édition, sinon celui de l'Œuvre.

## Bibliothèque de type Livres

Shelfhold ne crée ni ne modifie de bibliothèque Kavita. Une étagère d'Œuvres `novel`,
`light_novel` ou `ebook` doit viser une bibliothèque Kavita de type **Book**, déclarée par l'admin.
Sinon, le plan affiche l'avertissement `target.library_type_mismatch`. Ne mélangez pas EPUB et CBZ
dans une même étagère.
