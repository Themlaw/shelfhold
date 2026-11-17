# Webhooks

Un webhook envoie les événements d'exploitation de Shelfhold (un chapitre rangé, un disque presque plein, une sauvegarde en échec…)
à une adresse de votre choix, pour les brancher à un autre outil. Tout se fait par l'API, en tant qu'administrateur :
`POST /api/v1/webhooks`, `GET /api/v1/webhooks`, `GET /api/v1/webhooks/{id}`, `DELETE /api/v1/webhooks/{id}`.

## Qui peut en faire

- **Créer et supprimer** est une action réservée (`webhook.manage`) : une session d'administrateur récente, jamais une clé d'API.
- **Lire** la liste est réservé à l'administrateur : sa session, ou une clé de portée `admin` qui n'est pas en lecture seule.
- Un enfant, un membre, une clé en lecture seule, un jeton d'appareil ou personne reçoivent un refus avant que quoi que ce soit
  soit lu : un webhook qui existe et un webhook qui n'existe pas reçoivent la même réponse.

## L'adresse

- Une adresse **https publique**. L'adresse est vérifiée à l'enregistrement **et à chaque envoi** : le nom est résolu de nouveau
  à chaque fois, et Shelfhold ne se connecte qu'à l'adresse qu'il vient de valider. Un nom qui répondait une adresse publique à
  l'enregistrement et répond une adresse privée plus tard est refusé (l'envoi est repoussé, rien ne part).
- Les adresses privées, locales ou de métadonnées de nuage (la boucle locale, les plages `10.x` et `192.168.x`, la plage « lien local » des services de métadonnées…) sont refusées.
  Pour un serveur de **votre** réseau, dites-le explicitement avec `"allow_private": true` : c'est alors cet hôte et ce port
  exactement, avec les adresses qu'ils avaient ce jour-là (un enregistrement DNS qui bouge vers une autre machine est refusé).
  L'adresse de métadonnées de nuage reste refusée quoi que vous autorisiez.
- Les redirections ne sont jamais suivies : une réponse 3xx est un envoi manqué.
- Seul l'hôte est montré par la suite : le chemin d'un webhook est souvent un jeton.

## Le secret et la signature

Le secret est donné **une seule fois**, dans la réponse à la création. Il est scellé dans la base (avec l'adresse), n'apparaît
dans aucune autre réponse ni dans aucun journal. Si vous le perdez, supprimez le webhook et refaites-en un.

Chaque envoi est un `POST` JSON signé : `X-Shelfhold-Signature: t=<secondes unix>,v1=<HMAC-SHA256 hexadécimal>`, où la valeur
signée est `"<t>."` suivi des octets exacts du corps, avec le secret pour clé. Refusez un `t` trop ancien (cinq minutes) pour
vous protéger du rejeu. Les autres en-têtes : `X-Shelfhold-Event` (le type), `X-Shelfhold-Delivery` (l'identité de l'envoi,
**la même à chaque reprise** : ignorez une seconde copie), `X-Shelfhold-Schema-Version`.

Le corps : `event_id`, `type`, `schema_version`, `occurred_at`, `data`. `data` ne contient que des identifiants, des nombres et
des codes : jamais un titre, ni un chemin, ni un nom de personne. Ce que reçoit un webhook est ce que l'historique d'activité
montre au rôle « intégration » : aucun événement privé n'en part.

## Reprises

Les envois en attente sont des lignes de la base : un arrêt brutal du serveur n'en perd aucun. Un envoi manqué est repris au
bout de 30 s, puis 60 s, 120 s… (jusqu'à 6 h), huit fois au plus ; passé cela il est abandonné. Une réponse 2xx règle l'envoi.

## Versions

Chaque type d'événement a une version de sa forme (`schema_version`). Quand une forme change, l'ancienne est **dépréciée** :
pendant 180 jours au plus, chaque événement est envoyé **deux fois** (l'ancienne version, puis la courante) pour que vous
puissiez migrer sans rien manquer ; ensuite, la version courante seule. La liste des types, de leurs champs et des dépréciations
en cours est dans la description de l'API (`x-webhooks`).
