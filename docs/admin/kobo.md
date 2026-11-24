# Kobo : synchronisation minimale (L17a)

Shelfhold parle le protocole de synchronisation d'une liseuse Kobo, de façon minimale : la
liseuse voit la bibliothèque de **son profil** et peut la synchroniser page par page. C'est la
partie livrée pour M6 ; la suite (état de lecture, proxy du store) est coupée et décrite à la fin.

## Routes

- `GET /kobo/{token}/v1/initialization`
- `POST /kobo/{token}/v1/auth/device`
- `GET /kobo/{token}/v1/library/sync`
- `/{token}/{rest:path}` (stub, `{}` ; `404` pour `v1/download/` et `v1/covers/`)

## Ce qui est livré
- **Trois routes réelles**, sous le jeton d'appareil de la liseuse :
  - `GET /kobo/{token}/v1/initialization` : les ressources de ce serveur, et de nul autre ;
  - `POST /kobo/{token}/v1/auth/device` : activation factice, acceptée, rien de stocké (la
    liseuse est déjà appairée, son jeton est le segment de chemin) ;
  - `GET /kobo/{token}/v1/library/sync` : une page de la bibliothèque, avec l'en-tête
    `x-kobo-synctoken` pour continuer et `X-Kobo-sync: continue` tant qu'une page suit.
- **Toute autre route** sous le jeton répond `{}` pour toutes les méthodes, pour qu'aucune route
  non documentée ne devienne une erreur sur laquelle la liseuse s'arrête.
- **Téléchargement et couverture** répondent `404` tant que la partie non livrée n'est pas là :
  un `200` vide dirait à la liseuse qu'elle détient un fichier qu'elle n'a pas.
- **Identifiant** : le jeton d'appareil de la liseuse, dans le chemin. Le profil est le périmètre
  de toutes les requêtes, lu en base à chaque appel.
- **TLS obligatoire** : une liseuse ancienne qui refuse le TLS moderne est traitée comme dans
  [liseuses-tls.md](liseuses-tls.md). Les réponses portent `Cache-Control: no-store`.
- Les adresses que les réponses portent sont **relatives** au point de montage : l'adresse que la
  liseuse tient déjà, jamais une construite depuis `Host`.

## Ce qui n'est pas livré (L17b, coupé pour M6)

- L'**état de lecture** de la liseuse (position, progression) n'est pas synchronisé par cette
  route : la liseuse lit, Shelfhold ne reçoit rien de sa place.
- Le **proxy du store** n'existe pas : aucune requête ne quitte vers le store de Kobo, aucune
  ressource d'un autre serveur n'est relayée.
- Le **téléchargement des octets** d'un livre et sa **couverture** ne sont pas servis : la
  métadonnée les nomme, mais ils répondent `404`.
- Le **dossier Kobo** (destination d'export) est une autre surface, qui réutilise la même
  déclaration : voir [destinations.md](destinations.md).

## Mettre une liseuse en service

1. Créez un **jeton d'appareil** pour le profil qui lit (voir [jetons-appareil.md](jetons-appareil.md)).
2. Donnez à la liseuse l'adresse `https://<instance>/kobo/<jeton>` (le préfixe de montage inclus).
3. La liseuse doit atteindre cette adresse en HTTPS, avec un certificat qu'elle accepte : pour les
   liseuses anciennes, voir [liseuses-tls.md](liseuses-tls.md).
4. Les livres que le profil peut voir apparaissent au fil des pages ; la liseuse demande la suite
   avec `x-kobo-synctoken`.

## Ce que Shelfhold ne fait pas

- Aucune requête ne part vers un autre serveur : les ressources de l'initialisation sont celles de
  ce serveur, et de nul autre.
- Rien n'est mis en cache : la bibliothèque change avec un plafond, une étagère ou un masquage.

Voir aussi [clients.md](clients.md), [lite.md](lite.md), [opds.md](opds.md).