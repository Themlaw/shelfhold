# Liseuses et TLS : joindre Shelfhold depuis un appareil ancien

Shelfhold exige HTTPS pour tout ce qui porte le secret d'une liseuse : `/opds`, `/kosync` et
`/lite`. En HTTP simple, ces routes refusent la requête **avant** de lire le moindre secret
(`device_tls_required`), parce que ce secret circulerait en clair et se rejouerait tel quel.

Le problème n'est donc pas Shelfhold : c'est le **magasin de certificats** de la liseuse. Un
appareil dont le magasin est figé au firmware ne connaît pas les autorités de certification
récentes, et refuse la chaîne que votre serveur présente. La page dit ce que cela donne par famille
d'appareil, et quoi faire.

## Ce que chaque famille sait faire

| Appareil | Navigateur | TLS moderne | Ce qu'il faut faire |
| --- | --- | --- | --- |
| **Kobo** (navigateur natif) | WebKit 538.1, sans ES6 | magasin figé au firmware : une chaîne récente est refusée | `/lite` si la chaîne passe ; sinon **KOReader** |
| **Kindle** jusqu'à 5.12.4 | navigateur ancien, sans ES6 | magasin figé | **KOReader** |
| **Kindle** 5.16.4 et au-delà | Chromium embarqué (version non publiée) | dépend du firmware | `/lite` ou la SPA « au mieux » ; **KOReader** reste la voie sûre |
| **Boox** | navigateur intégré non documenté, Chrome installable | celui de Chrome | Chrome conseillé, ou **KOReader** |
| **Téléphone, tablette, ordinateur** | navigateur récent | à jour | la SPA, `/lite`, ou **KOReader** |

Aucune de ces lignes n'affirme qu'un appareil a été essayé : la matrice de [clients.md](clients.md)
ne passe de « non vérifié » à un fait que par un **rapport d'appareil**
(`.github/ISSUE_TEMPLATE/rapport-appareil.yml`), rempli par un volontaire, avec le User-Agent relevé.

## KOReader est la voie conseillée

**KOReader est conseillé** pour toute liseuse e-ink, et c'est la seule voie qui ne dépende pas du
magasin de certificats du firmware : il embarque le sien et se met à jour. Il parle l'OPDS de
Shelfhold (catalogue) et son kosync (progression), avec le même jeton d'appareil
([opds.md](opds.md), [kosync.md](kosync.md)). Une liseuse qui ne peut pas joindre l'instance en TLS
moderne n'a donc pas besoin d'un réglage serveur : elle a besoin de KOReader.

## Les trois issues, dans l'ordre

1. **KOReader** (recommandé). Rien à régler côté serveur.
2. **Une chaîne que la liseuse accepte.** Si votre mandataire inverse présente une chaîne dont
   l'autorité est connue du firmware de l'appareil, le navigateur natif s'ouvre et `/lite`
   fonctionne. C'est un choix de votre hébergement, pas de Shelfhold ; vérifiez-le sur l'appareil
   lui-même, pas dans un navigateur de bureau.
3. **HTTP simple sur un réseau local de confiance** (dernier recours).
   `SHELFHOLD_DEVICE_ALLOW_PLAIN_HTTP=true` (faux par défaut) lève le refus. Il est réservé à un
   **réseau local de confiance** : quiconque lit ce réseau lit le secret de la liseuse et rejoue le
   condensé que kosync envoie. Ne l'activez jamais sur une instance exposée sur Internet.

Derrière un mandataire inverse, déclarez-le dans `SHELFHOLD_HTTP_TRUSTED_PROXIES` : `X-Forwarded-Proto`
n'est lu que de lui. Sans cette déclaration, toutes les requêtes semblent venir en clair et les
routes des liseuses refusent tout, même si le client est bien en HTTPS.

## Le sous-domaine, et le cookie de `/lite`

Le cookie de `/lite` porte `Path={préfixe}/lite`, ce qui interdit le préfixe `__Host-`. Hébergez
Shelfhold sur un **sous-domaine qui ne partage pas son domaine parent avec un service non fiable** :
un sous-domaine voisin sous HTTPS pourrait sinon poser un cookie `__Secure-lite` sur le domaine
parent. Pour que cela serve à quelque chose, il faudrait en plus qu'il tienne un jeton de cette
instance — c'est donc un membre du foyer qui contrôle aussi ce voisin.

## Le risque résiduel R66 : un fichier sorti ne revient pas

Un fichier téléchargé par `/lite` ou par l'OPDS est **sorti** de Shelfhold : il vit sur la liseuse,
dans son stockage, et Shelfhold **ne peut pas** le **reprendre**. Si l'œuvre est ensuite purgée,
reclassée au-dessus du plafond d'un enfant ou retirée d'une étagère, la copie déjà téléchargée reste
lisible sur l'appareil. C'est le risque résiduel **R66**, accepté et documenté
([06-points-discutables.md](../spec/06-points-discutables.md)) : seule la copie hors ligne de la
PWA est sous contrôle de Shelfhold, et elle n'est pas encore livrée.

Ce que cela demande :

- **Retirer l'appareil** quand une liseuse est perdue, prêtée ou vendue : cela ferme les
  téléchargements suivants, jamais les fichiers déjà pris.
- **Ne pas compter sur un reclassement** pour retirer un livre d'une liseuse.
- Sur une liseuse partagée, **se déconnecter** en partant : la limite d'âge s'attache au compte, pas
  à l'appareil.

## Vérifier, et rapporter

1. Ouvrez `https://<votre-adresse>/lite` dans le navigateur de la liseuse. Une page de connexion qui
   s'affiche veut dire que le TLS passe ; une erreur de certificat veut dire qu'il ne passe pas.
2. Si le TLS ne passe pas, essayez KOReader avant de toucher au serveur.
3. Remplissez un **rapport d'appareil** : modèle, version du système, client et sa version,
   User-Agent relevé, colonne « hors ligne », formats d'image. C'est ce qui fait avancer la matrice
   de [clients.md](clients.md), et rien d'autre.
