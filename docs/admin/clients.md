# Clients supportés : matrice, hors ligne, OPDS

Cette page dit avec quoi on lit ce que Shelfhold range. Kavita reste **le lieu de lecture** du foyer
(web, OPDS, synchronisation de KOReader) ; Shelfhold ajoute depuis M5 son propre lecteur, ses propres
flux OPDS et kosync, et la page `/lite` pour les liseuses sans JavaScript.

## Ce que cette matrice affirme, et ce qu'elle ne sait pas

Une case n'affirme que ce qu'on peut vérifier dans la documentation d'un client ou dans le code de
Shelfhold. Sinon elle dit **« non vérifié »**. Aucune case ne prétend qu'un appareil a été essayé :
les vérifications sur appareil réel (une par famille, avec le User-Agent relevé) se font par le
gabarit d'issue **« rapport d'appareil »** (`.github/ISSUE_TEMPLATE/rapport-appareil.yml`). Une case
« non vérifié » ne veut pas dire « non » : elle veut dire que personne n'a encore pu le prouver.

La colonne **Hors ligne** répond à une seule question : ce client sait-il lire sans réseau, et qui
garde la copie ?

| Client | Usage | Formats d'image | Hors ligne | Remarque |
| --- | --- | --- | --- | --- |
| Navigateurs récents (Chrome, Firefox, Edge, Safari) | SPA de Shelfhold, lecteur intégré, lecteur web de Kavita | JPEG et PNG attendus ; WebP : non vérifié | non vérifié ; la coquille de la PWA s'ouvre sans réseau ; un livre gardé hors ligne se lit sans réseau ([copies hors ligne](../famille/copies-hors-ligne.md)) | la SPA a besoin du serveur pour tout le reste |
| Safari iOS | idem | non vérifié | non vérifié | à confirmer par un rapport d'appareil |
| Boox | navigateur Chrome conseillé, ou KOReader | non vérifié | non vérifié | écran e-ink : animations réduites |
| Kindle (navigateur récent, version 5.16.4 ou plus) | `/lite`, ou la SPA « au mieux » | non vérifié | non vérifié | « SPA au mieux » : peut fonctionner, sans garantie |
| Kobo (navigateur natif) | `/lite` | non vérifié | non vérifié | le KEPUB est proposé pour cette famille d'appareils |
| KOReader | OPDS et kosync de Shelfhold, ou ceux de Kavita | non vérifié | source : la page OPDS de Kavita le liste comme client OPDS ; le téléchargement n'y est pas décrit | la voie conseillée pour une liseuse e-ink |
| Kover | OPDS | non vérifié | non vérifié | absent de la liste de clients de la page OPDS de Kavita |

Autres clients de la liste de Kavita (Librera, Moon Reader, Kubo Reader, Chunky, Panels, Yomu,
Paperback) : cette page ne les teste pas et ne les recommande pas. La documentation de Kavita dit que
Mihon et CDisplayEx n'utilisent pas OPDS.

## Ce que M5 a livré, et où le lire

- **`/lite`** : pages HTML rendues par le serveur, **sans JavaScript**, pour le navigateur natif
  d'une Kobo (WebKit 538.1) et les Kindle jusqu'à 5.12.4. Parcourir, télécharger, se connecter avec
  un jeton d'appareil. Voir [lite.md](lite.md).
- **Le lecteur intégré** : un lecteur secondaire, pour un extrait ou pour une œuvre non exportée.
  Voir [../famille/lecteur.md](../famille/lecteur.md).
- **L'OPDS de Shelfhold**, en 1.2 (Atom) et en OPDS 2.0 (JSON), avec la lecture page à page
  (OPDS-PSE) et le bloc d'une série. Voir [opds.md](opds.md).
- **Le kosync de Shelfhold** : la progression d'un KOReader. Voir [kosync.md](kosync.md).
- **Le KEPUB** : un tome EPUB converti pour une Kobo, proposé dans l'OPDS et dans `/lite`, gardé en
  cache et purgé avec sa source. Un Kobo le lit mieux qu'un EPUB (pagination, couverture).
- **La coquille hors ligne (PWA)** : l'application s'installe et s'ouvre sans réseau. Elle ne garde
  **aucune** réponse de l'API. Un livre que le membre garde hors ligne est copié pour **son**
  profil seulement, et effacé à la déconnexion ou quand il ne lui est plus visible : voir
  [copies hors ligne](../famille/copies-hors-ligne.md).

## Détection de capacités, jamais d'User-Agent

L'interface décide de ce qu'elle affiche d'après ce que le navigateur **sait faire** (détection de
capacités), jamais d'après son User-Agent. Le User-Agent n'est relevé qu'à titre d'information, dans
un rapport d'appareil que vous remplissez vous-même.

## Le risque résiduel R66 : un fichier sorti ne revient pas

Un fichier téléchargé par `/lite` ou par l'OPDS est **sorti** de Shelfhold : il vit sur la liseuse,
et Shelfhold ne peut pas le reprendre. Une œuvre purgée, reclassée au-dessus du plafond d'un enfant
ou retirée d'une étagère reste donc lisible sur l'appareil qui l'a déjà prise. C'est le risque
résiduel **R66**, accepté et documenté ([06-points-discutables.md](../spec/06-points-discutables.md)) :
seule la copie hors ligne de la PWA serait sous contrôle de Shelfhold, et elle n'est pas livrée.

La réponse à une liseuse perdue est donc **retirer l'appareil** ([jetons-appareil.md](jetons-appareil.md)),
jamais compter sur un reclassement. Les liseuses anciennes et le TLS sont traités dans
[liseuses-tls.md](liseuses-tls.md).

## OPDS de Kavita avec un compte restreint

Kavita donne à chaque compte une adresse OPDS et une clé qui lui sont propres (documentation de
Kavita : « a unique URL and Auth key »). Pour un enfant, c'est cette adresse, jamais celle d'un
compte adulte, qu'on saisit dans le lecteur. Ce que la documentation de Kavita ne dit pas : si
l'adresse d'un compte restreint par l'âge ou par bibliothèque ne montre **que** ce que ce compte voit
dans le web. C'est **non vérifié** ici.

Checklist à jouer vous-même avant de donner l'adresse à un enfant :

1. Créez le compte de l'enfant (sans droit d'administration, bibliothèque famille seule, restriction
   d'âge, « inclure les inconnus » désactivé : voir [famille.md](famille.md)).
2. Ouvrez l'adresse OPDS **de ce compte** dans un client OPDS.
3. Cherchez une série réservée aux adultes, par son titre et par navigation : elle ne doit apparaître
   **nulle part** (listes, recherche, dernières ajoutées).
4. Si elle apparaît, n'utilisez pas OPDS pour cet enfant et ouvrez une issue (sans nom de site ni
   contenu protégé).
5. Retenez la clé comme un mot de passe : révoquez-la dans Kavita si l'appareil est perdu.

Shelfhold audite les comptes Kavita qu'il **lit** ; il ne voit pas ce que sert OPDS
([famille.md](famille.md)). L'OPDS de Shelfhold, lui, applique le filtre du contrôle parental à
chaque requête ([opds.md](opds.md)).
