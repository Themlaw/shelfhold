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
- Une Œuvre ouvre ses **Éditions**, chacune avec une entrée « Série complète » (le bloc d'une série
  en un seul fichier, lot suivant). Le flux ne propose pas le téléchargement d'un chapitre seul : le
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

## Pour l'intégrateur : brancher le bloc d'une série

Le téléchargement est un point d'extension : `shelfhold.interfaces.http_api.opds.bundle`
(`SeriesBundleProvider`, `BundleOffer`, `BundleDownload`), fourni par `main.series_bundles` (aucun
fournisseur tant que le lot du bloc n'est pas fusionné ; le flux ne propose alors aucun lien de
téléchargement). La route vérifie, à chaque requête, que l'Édition est visible pour le profil du jeton
avant de demander quoi que ce soit au fournisseur, et nettoie le nom de fichier qu'il donne.
