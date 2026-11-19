# Le flux OPDS de Shelfhold

Shelfhold sert un catalogue OPDS 1.2 (Atom) à une liseuse : KOReader, ou tout client OPDS. Il est
derrière un [jeton d'appareil](jetons-appareil.md) et rien d'autre : ni une session du site, ni une
clé d'API, ni le mot de passe d'un compte n'y ouvrent quoi que ce soit.

## Se connecter

Dans KOReader : *Recherche → Catalogue OPDS → +*, puis :

- adresse : `https://<votre-adresse>/opds` (en HTTPS : voir plus bas) ;
- nom d'utilisateur : l'**identifiant** du jeton (8 caractères) ;
- mot de passe : le **secret** du jeton (26 caractères, saisi tel quel).

Le jeton est celui d'**un profil**. Le flux montre exactement ce que ce profil voit dans Shelfhold,
relu à **chaque requête** : un plafond abaissé, un classement changé ou un jeton révoqué jouent dès la
page suivante.

## Ce que le flux montre

- **Étagères**, **Séries**, **Toutes les œuvres**, **Nouveautés** et la **recherche**.
- Une Œuvre ouvre ses **Éditions**, chacune avec une entrée « Série complète » : le bloc de la série
  en un seul fichier (voir plus bas). Le flux ne propose pas le téléchargement d'un chapitre seul : le
  catalogue de Kavita le fait déjà.
- Une couverture, quand l'Édition en a une.

Ce qu'un profil enfant ne voit pas **n'existe pas** pour lui : pas d'entrée, pas de compteur, pas de
résultat de recherche, pas de couverture, pas de lien de téléchargement. Une adresse d'Œuvre, d'Édition,
de série, d'étagère ou de couverture qu'il n'a pas le droit de voir répond comme une adresse inconnue
(404). Les totaux (`totalResults`) ne comptent que ce qu'il voit, et le lien « page suivante » n'existe
que s'il reste une entrée qu'il voit.

**Étagères** : le nom d'une étagère est une donnée du foyer. Un profil avec plafond ne voit que les
étagères qui déclarent un public sous la tranche 18+ et qui portent au moins une Œuvre qu'il voit. Un
adulte les voit toutes. Les Œuvres d'une étagère qu'on ne lui montre restent atteignables par
« Toutes les œuvres », « Séries » et la recherche, selon son plafond.

## HTTPS, mandataire et sous-chemin

HTTPS est exigé, comme pour kosync (`device_tls_required` en HTTP simple, avant toute vérification du
secret). Derrière un mandataire inverse, déclarez-le dans `SHELFHOLD_HTTP_TRUSTED_PROXIES` :
`X-Forwarded-Proto` et, pour un site servi sous un sous-chemin, `X-Forwarded-Prefix` ne sont lus que
de lui.

Tous les liens du flux sont des chemins qui commencent par le préfixe (`root_path` du serveur, ou
`X-Forwarded-Prefix` d'un mandataire de confiance) puis `/opds`. Aucun ne porte une adresse construite
d'après l'en-tête `Host`, un identifiant, un secret ou un titre. Un préfixe qui n'est pas un chemin
simple (`//autre-hote`, `..`, un espace) est ignoré.

## Le bloc d'une série

L'entrée « Série complète » donne **toute la série en un seul téléchargement** : un fichier CBZ
virtuel, fabriqué à la lecture à partir des fichiers déjà exportés. Rien n'est écrit dans la
bibliothèque ni dans Kavita, et aucune copie complète n'existe sur le disque.

- **Une version, un nouveau fichier.** Le titre porte un numéro : `Titre — Série complète v7.cbz`. Un
  chapitre ajouté, retiré ou remplacé donne `v8`, qui **s'ajoute** à `v7` dans la liseuse (KOReader lie
  la position au chemin du fichier : remplacer `v7` par un contenu différent lui ferait lire les
  mauvaises pages). Supprimez les anciennes versions quand vous voulez.
- **Pages lisibles seulement.** Le bloc contient des pages JPEG, PNG, GIF et WebP, rangées par chapitre
  (`00001 ch12/00001.png`) : KOReader en fait un sommaire. Un chapitre dont une page est dans un autre
  format (AVIF, JPEG XL, …), un chapitre exporté en EPUB ou en PDF, ou dont le fichier manque, est
  **laissé hors du bloc**, et l'entrée le dit (« 3 chapitres exclus »). `ComicInfo.xml` n'est pas
  recopié.
- **Parties.** Au-delà de 500 Mio, le bloc est coupé en parties : `Titre — Série complète (2 sur 3)
  v1.cbz`. Les limites de parties sont écrites quand la partie est créée et ne bougent plus : un
  chapitre ajouté va dans la dernière partie, un chapitre remplacé ne change que la sienne.
- **Tranches de plages fixes.** Sur la fiche de la série (« Bloc pour liseuse »), l'admin peut fixer
  un nombre de chapitres par partie (de 1 à 256) : `Titre — Série complète (2 sur 5) v1.cbz` couvre
  alors, par exemple, les chapitres 21 à 40. La tranche ne fait que **resserrer** la coupe : le poids
  maximal et le nombre de membres d'une partie restent des plafonds. Elle ne déplace **aucune limite
  déjà écrite** : seule la partie ouverte (la dernière) et celles qui s'y ajoutent sont recoupées, donc
  un fichier déjà sur la liseuse garde sa partie, et une partie dont le contenu ne change pas ne
  reçoit pas de nouvelle version. Retirer la tranche rend la coupe au poids. L'écran montre les
  parties prévues d'après le manifeste (rien n'est ouvert ni écrit) et le nombre de chapitres laissés
  hors du bloc. Seul l'admin la voit et la règle ; la tranche suit l'Édition dans l'export intégral.
- **Taille exacte, pas de reprise.** `Content-Length` est exact avant le premier octet. Il n'y a ni
  `Range` ni `206` : KOReader n'en envoie pas et efface le fichier sur tout code autre que 200.
- **Même octets, toujours.** À contenu égal, le fichier est identique octet pour octet, y compris
  après un redémarrage : mêmes noms, dates fixées, aucun champ variable. L'empreinte que KOReader
  calcule (douze échantillons d'un Kio) tient compte d'une identité de version écrite dans le
  premier Kio.

**Qui reçoit quoi.** Le bloc est celui du **profil du jeton**. Un enfant ne reçoit que l'Édition qu'il
voit, sans aucun chapitre d'une Édition masquée ; une série masquée n'a pas d'entrée. L'accès est
revérifié à **chaque requête** et **pendant le transfert** : jeton révoqué ou expiré, Édition
reclassée au-dessus du plafond, et le transfert s'arrête au bloc d'octets suivant (la liseuse
supprime alors le fichier partiel). Une version d'un autre profil répond comme une version inconnue.

**Quand le serveur répond 410.** Un fichier ne correspond plus à ce que le manifeste décrit, ou la
version a été purgée par la rétention : rien n'est envoyé. Rouvrez la série dans la liseuse, une
version à jour y est proposée. Un fichier remplacé **pendant** un transfert ne mélange pas deux
contenus : l'ancien reste lu jusqu'au bout (le descripteur est ouvert dès le début), et un fichier
réécrit sur place arrête le flux.

**Limites.** Un seul bloc à la fois par jeton, deux pour l'instance (`429` avec `Retry-After`
au-delà), 256 chapitres par partie, un lecteur qui reçoit moins de 32 Kio/s pendant 60 s est coupé. La
mémoire d'un flux est de l'ordre de quelques morceaux de 64 Kio, quelle que soit la taille du bloc.

Le créneau d'un téléchargement est un **bail**. Il est rendu à la fin du transfert, à son échec, et
dès que le client coupe sa connexion (la route ferme le flux, sans attendre le ramasse-miettes). Il
expire aussi tout seul : un client qui ne lit plus rien (fenêtre TCP à zéro) garde son créneau au
plus `send_timeout_s` (120 s par défaut) sans progrès, puis le prochain téléchargement le reprend, ses
descripteurs sont fermés et son flux s'arrête. Un transfert ne dure jamais plus de `max_duration_s`
(8 h), quel que soit son débit. Ces deux durées, comme les autres bornes, sont dans `BundleLimits`
(`reading/bundle/limits.py`).

**Chapitre au CRC faux.** Avant de fabriquer une nouvelle version, chaque page est lue une fois et
son CRC-32 comparé à celui que l'archive annonce. Un chapitre dont l'archive ment est **laissé hors
du bloc** (« non lisible ») et le reste est servi, au lieu de couper chaque téléchargement à la même
page.

**Historique, sauvegarde et restauration.** Les limites des parties et les numéros de version sont
un **historique**, pas un cache. Un numéro n'est jamais servi deux fois pour la même Édition, la
même partie et le même profil, même quand une limite disparaît (la série a été recoupée) : les
versions de cette partie sont retirées (410) mais leurs lignes restent, et la suivante porte le
numéro d'après. L'export intégral (`shelfhold export-all`) emporte `bundle_partition`,
`bundle_version` et les alias des parties ; une restauration retrouve donc les mêmes parties, les
mêmes numéros et les mêmes empreintes KOReader. Les membres de chaque version (des empreintes de
fichiers) restent derrière : le prochain téléchargement les refait.

**Rétention.** Une version est créée au premier téléchargement d'un contenu. On garde les 3 dernières
par (Édition, partie, profil) et toute version utilisée depuis moins de 90 jours ; le reste est purgé
chaque jour (aucune empreinte de fichier n'est conservée). Ce que la purge de la bibliothèque
supprime emporte aussi l'empreinte des chapitres concernés ; le rapport de purge de `replica` ne voit
pas les copies déjà livrées aux liseuses (elles sont hors de portée).

## Lire page à page (OPDS-PSE)

Les applis qui parlent OPDS-PSE (Panels, Chunky, Kybook, KOReader et d'autres) lisent un tome sans le télécharger. Sur la page d'une Œuvre du flux, chaque tome exporté a une entrée « lecture page à page » avec un lien `pse:stream` : l'adresse contient `{pageNumber}`, que l'appli remplace par le numéro de la page, **à partir de 0**. `pse:count` donne le nombre de pages.

- **Le jeton d'appareil suffit**, comme pour le reste de `/opds`. Une session web ou une clé d'API est refusée (`401`).
- **Le profil est relu à chaque page.** Un profil enfant ne reçoit jamais une page d'une Œuvre au-dessus de son plafond, et la réponse est la même que pour un tome qui n'existe pas (`404`). Si l'Œuvre est reclassée ou si le jeton est révoqué pendant la lecture, la page suivante s'arrête.
- **Le format suit l'en-tête `Accept`.** Si l'appli accepte le format de la page, elle le reçoit tel quel. Sinon, une page AVIF ou WebP est convertie en JPEG ou en PNG (un seul calcul, gardé en cache). Si rien ne convient, la réponse est `406` avec la marche à suivre. Sans `Accept`, la page est envoyée telle quelle.
- **Lecture seule.** Shelfhold n'écrit aucune position en servant une page, et le flux n'annonce pas `pse:lastRead`. La position se synchronise par kosync.
- Seuls les tomes dont le fichier conservé est un CBZ lisible sont proposés. Un fichier abîmé ou absent n'a pas d'entrée.

## Pour l'intégrateur : le fournisseur du bloc

Le téléchargement passe par `shelfhold.interfaces.http_api.opds.bundle` (`SeriesBundleProvider` :
`offers` pour le flux, `open` pour le téléchargement), fourni par `main.series_bundles` (implémenté par
`shelfhold.reading.bundle.SeriesBundles`). La route vérifie, à chaque requête, que l'Édition est
visible pour le profil du jeton avant de demander quoi que ce soit au fournisseur, et nettoie le nom de
fichier qu'il donne ; le fournisseur revérifie chaque chapitre.
