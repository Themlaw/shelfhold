# La route `/content` : les octets d'un livre

Le lecteur intégré ne reçoit jamais un fichier entier : il demande une **entrée** (une page, un
chapitre XHTML, une image, une feuille de style) et le serveur la lui sert par une seule porte,
`GET /content/{ticket}/{path}`. C'est la seule route qui sorte un octet de livre, et elle est
fermée par défaut : sans ticket valide, elle ne dit rien.

## Le ticket

Un ticket est délivré par le manifeste de la publication
(`GET /api/reader/{type}/{id}/publication`), pour un profil et **la session qui le demande**. Il est
scellé (XChaCha20-Poly1305, clé de l'instance) et porte :

- le **profil** et l'empreinte de sa **session** ;
- le **support** (le fichier conservé, ou l'export) et son identifiant ;
- la **portée** : `full` (tout le livre) ou `excerpt` (la liste fermée des entrées coupées) ;
- une **échéance de 10 minutes** (600 s), renouvelée par le lecteur quand il recharge le manifeste.

Le ticket est dans le chemin, pas dans un en-tête : il ne part pas dans un journal de mandataire
avec un cookie, et il n'est pas réutilisable ailleurs. Un ticket forgé, expiré, d'un autre profil,
d'un autre support ou d'une autre portée répond **404**, comme une entrée qui n'existe pas : il n'y
a ni oracle, ni balise, ni longueur qui distingue les cas.

## Ce que la route refuse

| Cas | Réponse |
| --- | --- |
| ticket forgé, expiré, autre profil, autre support, autre portée | `404` (`not_found`) |
| entrée hors de la liste d'un extrait, ou nom qui remonte (`..`, `%2e%2e`) | `404` |
| profil qui ne voit plus le support (reclassement, étagère retirée) | `404` |
| clé d'API ou jeton d'appareil présenté | `403` (`credential_not_allowed`) |
| fichier remplacé depuis le ticket (portée `full`) | `409` (`content_changed`) : rechargez le manifeste |
| aucune copie lisible | `409` (`no_readable_copy`) |
| extrait demandé là où rien ne peut être coupé | `409` (`excerpt_unavailable`) |
| lecture complète demandée pour un fichier lu ailleurs | `409` (`full_read_not_allowed`) |

L'autorisation est **complète avant** que l'`ETag` soit comparé ou qu'un octet soit lu : un
`If-None-Match` qui tombe juste ne sert pas un fichier qu'on n'a plus le droit de voir.

## Les en-têtes de chaque réponse

- `Content-Security-Policy: sandbox` — toujours, quel que soit le type.
- `Content-Disposition: attachment` — sauf pour une image matricielle (`image/png`, `image/jpeg`,
  `image/webp`, `image/gif`, `image/avif`), servie en ligne. Un SVG, un XHTML, un CSS partent donc
  en pièce jointe : rien ne s'affiche dans l'origine de l'application.
- `Cache-Control: private` et un `ETag` (l'empreinte des octets servis), pour que le lecteur ne
  redemande pas une page qu'il a déjà.
- `X-Content-Type-Options: nosniff`, `Referrer-Policy` et `frame-ancestors` viennent du middleware.

Les octets servis sont ceux du **dérivé neutralisé** d'un EPUB non fiable, jamais l'original de la
source ([neutraliseur.md](neutraliseur.md)). Un fichier protégé n'entre pas dans la bibliothèque :
il est refusé à l'import, et rien n'est servi pour lui.

## L'origine séparée (facultative)

Par défaut, `/content` vit sur l'origine de l'application : le cookie de session dit qui demande, et
le ticket doit correspondre à cette session. C'est le mode recommandé, et le seul qui ne demande
aucun réglage.

`SHELFHOLD_CONTENT_ORIGIN` place les octets des livres sur une **origine séparée** (un schéma et un
hôte, sans chemin) : `https://content.example.org`. Le navigateur n'envoie alors aucun cookie, et le
serveur vérifie l'empreinte de session du ticket contre les sessions **vivantes** du profil. Ce mode
isole complètement le contenu du livre de l'origine de l'application — utile si vous voulez que
rien d'un EPUB ne partage jamais l'origine qui porte vos cookies.

Ce qu'il demande :

- un second nom d'hôte qui pointe sur la même instance, et le mandataire qui le déclare
  (`SHELFHOLD_HTTP_TRUSTED_PROXIES`) ;
- l'origine de `SHELFHOLD_PUBLIC_URL` déclarée comme seule origine autorisée à lire ces octets
  (`Access-Control-Allow-Origin`, sans identifiants) ;
- un `OPTIONS` de pré-vérification, que la route répond elle-même.

Sans `SHELFHOLD_CONTENT_ORIGIN`, rien de tout cela n'existe : aucune réponse ne porte d'en-tête
CORS, et une requête sans cookie est refusée.

## L'extrait

Un extrait est **calculé par le serveur** à partir du fichier qu'il va servir, jamais donné par
l'appelant : au plus **10 pages** et au plus **un cinquième** (20 %) du livre pour un support de
pages ; pour un EPUB, les premiers documents dont la taille cumulée atteint ce cinquième. Un extrait
**n'écrit aucune position** (`excerpt_writes_no_position`) : il ne compte pas pour « Reprendre la
lecture » et ne part jamais vers un serveur de lecture.

## Pour l'intégrateur

La route est `shelfhold.interfaces.http_api.content_routes.build_content_router` ; la décision est
`shelfhold.reading.application.content_access.ContentAccess` (`issue`, `read`, `url`). Le manifeste
est `shelfhold.reading.application.publication.PublicationService`. Aucun autre module ne sert un
octet de livre : un contrat import-linter le tient.
