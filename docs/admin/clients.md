# Clients supportés : matrice, hors ligne, OPDS

Cette page dit avec quoi on lit ce que Shelfhold range. En V1, la lecture se fait **dans Kavita** (web, OPDS,
synchronisation de KOReader) ; l'interface de Shelfhold est une application web (SPA) pour gérer la
bibliothèque. Les chemins `/lite` et OPDS/kosync propres à Shelfhold arrivent en **M5**.

## Ce que cette matrice affirme, et ce qu'elle ne sait pas

Une case n'affirme que ce qu'on peut vérifier dans la documentation de Kavita ou d'un client OPDS. Sinon elle
dit **« non vérifié »**. Aucune case ne prétend qu'un appareil a été essayé : les vérifications sur appareil réel
(une par famille, avec le User-Agent relevé) sont **à venir**, et l'entrée se fait par le gabarit d'issue
« rapport d'appareil » (`.github/ISSUE_TEMPLATE/rapport-appareil.yml`). Une case « non vérifié » ne veut pas
dire « non » : elle veut dire que personne n'a encore pu le prouver.

La colonne **Hors ligne** répond à une seule question : ce client sait-il télécharger depuis Kavita en V1 ?

| Client | Usage en V1 | Formats d'image | Hors ligne | Remarque |
| --- | --- | --- | --- | --- |
| Navigateurs récents (Chrome, Firefox, Edge, Safari) | SPA de Shelfhold, lecteur web de Kavita | JPEG et PNG attendus ; WebP : non vérifié | non vérifié | la SPA a besoin du serveur |
| Safari iOS | idem | non vérifié | non vérifié | à confirmer par un rapport d'appareil |
| Boox | navigateur Chrome conseillé, ou KOReader | non vérifié | non vérifié | écran e-ink : animations réduites |
| Kindle (navigateur récent, version 5.16.4 ou plus) | SPA « au mieux » | non vérifié | non vérifié | « SPA au mieux » : peut fonctionner, sans garantie |
| Kobo (navigateur natif) | `/lite` (M5, à venir) | non vérifié | non vérifié | rien de prévu avant M5 pour le navigateur natif |
| KOReader | catalogue OPDS de Kavita, synchronisation de lecture | non vérifié | source : la page OPDS de Kavita le liste comme client OPDS ; le téléchargement n'y est pas décrit | |
| Kover | OPDS | non vérifié | non vérifié | absent de la liste de clients de la page OPDS de Kavita |

Autres clients de la liste de Kavita (Librera, Moon Reader, Kubo Reader, Chunky, Panels, Yomu, Paperback) :
cette page ne les teste pas et ne les recommande pas. La documentation de Kavita dit que Mihon et CDisplayEx
n'utilisent pas OPDS.

## Détection de capacités, jamais d'User-Agent

L'interface décide de ce qu'elle affiche d'après ce que le navigateur **sait faire** (détection de
capacités), jamais d'après son User-Agent. Le User-Agent n'est relevé qu'à titre d'information, dans un rapport
d'appareil que vous remplissez vous-même.

## OPDS de Kavita avec un compte restreint

Kavita donne à chaque compte une adresse OPDS et une clé qui lui sont propres (documentation de Kavita :
« a unique URL and Auth key »). Pour un enfant, c'est cette adresse, jamais celle d'un compte adulte, qu'on saisit dans
le lecteur. Ce que la documentation de Kavita ne dit pas : si l'adresse d'un compte restreint par l'âge ou par
bibliothèque ne montre **que** ce que ce compte voit dans le web. C'est **non vérifié** ici.

Checklist à jouer vous-même avant de donner l'adresse à un enfant :

1. Créez le compte de l'enfant (sans droit d'administration, bibliothèque famille seule, restriction d'âge, « inclure
   les inconnus » désactivé : voir [famille.md](famille.md)).
2. Ouvrez l'adresse OPDS **de ce compte** dans un client OPDS.
3. Cherchez une série réservée aux adultes, par son titre et par navigation : elle ne doit apparaître **nulle part**
   (listes, recherche, dernières ajoutées).
4. Si elle apparaît, n'utilisez pas OPDS pour cet enfant et ouvrez une issue (sans nom de site ni contenu protégé).
5. Retenez la clé comme un mot de passe : révoquez-la dans Kavita si l'appareil est perdu.

Shelfhold audite les comptes Kavita qu'il **lit** ; il ne voit pas ce que sert OPDS ([famille.md](famille.md)).
