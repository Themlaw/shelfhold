# La page `/lite` : lire la bibliothèque sans JavaScript

`/lite` est une seconde interface, **rendue par le serveur**, pour les liseuses dont le navigateur
n'exécute pas le JavaScript moderne : le navigateur natif d'une **Kobo** (WebKit 538.1) et les
**Kindle** jusqu'à la version 5.12.4. Elle sert à **parcourir et télécharger** ; elle ne lit pas un
livre à l'écran (voir [clients.md](clients.md) pour savoir avec quoi lire).

Elle ne partage rien avec l'application web : ni ses feuilles de style, ni son thème, ni son
script. Chaque page est du HTML écrit par le serveur, avec **aucun `<script>`** — un contrôle de CI
le vérifie — et une politique de sécurité qui n'autorise que la feuille de style de la page, nommée
par son empreinte (`default-src 'none'`, `img-src 'self'`, `form-action 'self'`, `base-uri 'none'`).
Une page qui contiendrait malgré tout une balise ne lancerait donc rien.

## Se connecter

L'unique identifiant de `/lite` est le **secret d'un jeton d'appareil** ([jetons-appareil.md](jetons-appareil.md)),
saisi une fois sur `/lite/sign-in` :

- **Identifiant** : les 8 caractères affichés à l'appairage ;
- **Secret** : les 26 caractères, saisis tels quels.

Ni une session du site, ni une clé d'API, ni une identité de mandataire n'ouvrent `/lite` : le
serveur ne les résout pas sur ce préfixe. Le cookie posé ne contient **aucun secret** : une preuve
signée pour un jeton, limitée à `Path={préfixe}/lite`, `HttpOnly`, `SameSite=Strict`, `Secure` et
préfixée `__Secure-lite` sous TLS (le nom est `lite` en HTTP simple). Il dure 30 jours.

À **chaque requête**, le jeton est relu : vivant, son appareil non retiré, son secret celui avec
lequel la session a été ouverte. Le profil est celui du jeton, lu dans la base à cet instant — un
rôle ou un plafond n'est jamais gardé dans le cookie. Un plafond abaissé, un classement changé, un
jeton révoqué ou un appareil retiré jouent donc dès la page suivante.

## Ce que `/lite` montre

| Adresse | Ce qu'elle donne |
| --- | --- |
| `/lite` | l'accueil : les quatre portes (étagères, séries, toutes les œuvres, nouveautés) et la recherche |
| `/lite/shelves`, `/lite/shelves/{id}` | les étagères ouvertes au profil, puis leurs œuvres |
| `/lite/series`, `/lite/series/{id}` | les séries, puis leurs œuvres |
| `/lite/works`, `/lite/recent` | toutes les œuvres visibles, les nouveautés |
| `/lite/search?q=…` | la recherche |
| `/lite/works/{id}` | une œuvre : ses éditions, le téléchargement, le KEPUB |
| `/lite/covers/{empreinte}/{taille}` | une couverture, en vignette |
| `/lite/editions/{id}/complete` | le bloc d'une série en un fichier (le même que l'OPDS) |
| `/lite/kepub/{id}` | le KEPUB d'un tome EPUB, pour une Kobo |
| `/lite/sign-in`, `/lite/sign-out` | les deux seuls formulaires |

25 entrées par page, avec « page précédente » et « page suivante ». Le filtre du contrôle parental
est une condition de **chaque** requête : une page, un compteur, un résultat de recherche, une
couverture et un téléchargement ne contiennent que ce que le profil peut voir, et rien n'est filtré
après coup. Ce qui est caché, ce qui n'existe pas et ce qui est mal formé donnent **la même page
404**, octet pour octet : aucune adresse ne dit si une œuvre existe.

Le téléchargement suit le chemin de l'OPDS : il n'est proposé, et servi, que pour une édition que
le lecteur voit et peut lire maintenant. Le fichier est le **dérivé neutralisé**, jamais l'original
d'une source non fiable, et il part en pièce jointe, sous `sandbox`. Un bloc trop demandé répond
429 avec `Retry-After` ; un bloc dont la version a été purgée répond 410.

## Se déconnecter n'est pas une révocation

Le bouton « Se déconnecter » efface le cookie de **ce navigateur**. Il ne révoque pas le jeton :
un cookie déjà copié reste valable jusqu'à l'échéance du jeton, et le secret est de toute façon
écrit en clair dans les réglages de la liseuse. **La réponse à une liseuse perdue ou suspecte est
la même que pour l'OPDS : retirer l'appareil** (`DELETE /api/v1/devices/{id}`, ou
`shelfhold admin device-tokens remove`), ce qui tue le cookie à la requête suivante. Voir
[jetons-appareil.md](jetons-appareil.md).

## HTTPS, mandataire et sous-chemin

HTTPS est **exigé** : en HTTP simple, `/lite` refuse la requête avant de lire le moindre secret
(`device_tls_required`), parce que le secret d'une liseuse circulerait en clair et se rejouerait
tel quel. Le seul réglage qui l'autorise est `SHELFHOLD_DEVICE_ALLOW_PLAIN_HTTP=true` (faux par
défaut), réservé à un **réseau local de confiance** : quiconque lit ce réseau lit le secret.

Derrière un mandataire inverse, déclarez-le dans `SHELFHOLD_HTTP_TRUSTED_PROXIES` : `X-Forwarded-Proto`
et, pour un site servi sous un sous-chemin, `X-Forwarded-Prefix` ne sont lus que de lui. Sans cette
déclaration, toutes les requêtes semblent venir en clair et `/lite` refuse tout.

**Sous-domaine.** Le cookie de `/lite` porte `Path={préfixe}/lite`, ce qui interdit le préfixe
`__Host-`. Hébergez donc Shelfhold sur un **sous-domaine qui ne partage pas son domaine parent avec
un service non fiable** : un sous-domaine voisin sous HTTPS pourrait sinon poser un cookie
`__Secure-lite` sur le domaine parent. Pour que cela serve à quelque chose, il faudrait en plus
qu'il tienne un jeton de cette instance — c'est donc un membre du foyer qui contrôle aussi ce
voisin.

## Ce qui n'existe pas encore

- **La copie hors ligne d'un livre.** La coquille de l'application web (PWA) s'ouvre sans réseau,
  mais un livre n'entre pas encore dans une copie hors ligne gérée par Shelfhold : c'est
  **pas encore disponible**. Ce que vous téléchargez par `/lite` ou par l'OPDS reste sur la
  liseuse, hors de portée de Shelfhold (voir plus bas).
- **Les vignettes en niveaux de gris** pour un écran e-ink : pas encore disponibles ; les
  couvertures sont servies en couleur.
- **La lecture à l'écran dans `/lite`** : elle n'est pas prévue. Pour lire, utilisez KOReader
  (OPDS et kosync) ou Kavita.

## Le risque résiduel R66 : un fichier sorti ne revient pas

Un fichier téléchargé par `/lite` ou par l'OPDS est **sorti** de Shelfhold : il vit sur la liseuse,
dans son stockage, et Shelfhold n'a aucun moyen de le reprendre. Si l'œuvre est ensuite purgée,
reclassée au-dessus du plafond d'un enfant ou retirée d'une étagère, la copie déjà téléchargée
**reste lisible** sur l'appareil. C'est le risque résiduel **R66**, accepté et documenté
([06-points-discutables.md](../spec/06-points-discutables.md)) : seule la copie hors ligne de la
PWA est sous contrôle de Shelfhold, et elle n'est pas encore livrée.

Ce que cela demande de votre part :

- **Retirer l'appareil** quand une liseuse est perdue, prêtée ou vendue : c'est ce qui empêche
  d'en télécharger d'autres, jamais ce qui efface celles déjà prises.
- **Ne pas compter sur un reclassement** pour retirer un livre d'une liseuse : il ferme l'accès aux
  téléchargements suivants, il ne touche pas au fichier déjà copié.
- Sur une liseuse partagée, **se déconnecter** en partant : la limite d'âge s'attache au compte,
  pas à l'appareil.

## Vérifier sur votre liseuse

Ouvrez `https://<votre-adresse>/lite` dans le navigateur de la liseuse. Si la page ne s'ouvre pas du
tout, c'est un problème de TLS et non de `/lite` : voir [liseuses-tls.md](liseuses-tls.md). Si elle
s'ouvre mais que la connexion est refusée, vérifiez l'identifiant et le secret (le secret se saisit
tel quel, sans espace). Un rapport d'appareil (`.github/ISSUE_TEMPLATE/rapport-appareil.yml`) est le
seul chemin vers la matrice de [clients.md](clients.md).
