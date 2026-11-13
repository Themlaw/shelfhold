# API : `?since=` sur les listes

*Forme figée avant le gel de l'API 1.0 (EF-INT-001, M3 L11).* Les listes qui portent un état à resynchroniser donnent un jeton `since` et acceptent `?since=<jeton>`.

## Listes

| Route | Qui | Corps |
|---|---|---|
| `GET /api/v1/works?limit=&offset=&since=` | tout profil connecté, session ou clé ; **la vue du lecteur** (un enfant ne reçoit que ce que son plafond laisse passer) | `{items: [{work_id, title, kind}], total, since, removed}` |
| `GET /api/v1/shelves?since=` | administrateur seulement | `{items: [{id, name, max_age_rating}], since, removed}` |

`since` est le jeton à envoyer à l'appel suivant. Sans `?since=`, `removed` est vide.

## Le jeton

- Opaque : `s3.<…>`, une chaîne de largeur fixe. Le client ne la lit pas, ne la fabrique pas, ne la modifie pas, ne la compare pas : deux jetons du même instant sont deux chaînes sans rapport (chiffrement authentifié XChaCha20-Poly1305, nonce aléatoire, clé dérivée du trousseau de l'instance).
- Adossé à la **séquence** de la table des événements, jamais à une heure. La position qu'il porte n'est pas lisible : elle ne dit rien de l'activité des autres profils.
- Propre à sa liste : un jeton de `/shelves` est refusé sur `/works`.
- Lié à la **portée du lecteur** (adulte, ou son plafond), à l'époque de restauration de l'instance et à l'historique des événements. Un plafond abaissé ou relevé, un rôle changé, une sauvegarde restaurée (en place ou ailleurs) : la réponse est `resync_required`, jamais une liste de retraits incomplète. Un jeton ne désigne pas son porteur : un autre lecteur de **même** portée reçoit sa propre vue ; un lecteur d'une autre portée reçoit `resync_required`.

## `removed`

Les identifiants sortis de la vue **de ce lecteur** depuis le jeton :

- les Œuvres supprimées ;
- les Œuvres qui ont cessé d'être visibles pour lui (une classification relevée, par exemple 18+ pour un enfant).

Limites, toutes testées : jamais un identifiant d'Œuvre que le lecteur ne voyait pas **au moment du jeton** ; jamais une Œuvre créée après le jeton ; jamais un titre. Un adulte voit tout, donc n'a que les suppressions.

**Ce que `removed` ne dit pas.** Il ne liste que des Œuvres (et, sur `/shelves`, des étagères). Une Édition qui cesse d'être visible alors que son Œuvre reste visible (un classement 18+ sur une seule Édition) n'y figure pas : un client hors ligne qui garde des Éditions les revalide autrement (flux d'événements typés, L12, ou rechargement). Le client ne déduit rien de l'absence d'une Édition dans `removed`.

## Erreurs

| Statut | Code | Quand | Que faire |
|---|---|---|---|
| 400 | `invalid_cursor` | jeton qui ne s'ouvre pas : modifié, tronqué, d'une autre liste, d'une autre instance, d'un ancien format, trop long | ne pas réessayer ce jeton ; recharger la liste |
| 410 | `resync_required` | jeton plus ancien que la rétention des événements ; instance restaurée (époque ou historique différents, position pas encore émise) ; portée du lecteur changée (plafond, rôle) ; purge d'une Œuvre (tous les jetons antérieurs) ; plus de 1000 retraits | recharger la liste (sans `since`), garder le `since` de cette réponse |
| 401 / 403 | | pas de session ; `/shelves` demandée par un non-administrateur | |

La rétention des événements est de 90 jours par défaut (`retention_events_days`) : un client qui ne se synchronise pas pendant plus longtemps recharge la liste.
