# API : `?since=` sur les listes

*Forme figée avant le gel de l'API 1.0 (EF-INT-001, M3 L11).* Les listes qui portent un état à resynchroniser donnent un jeton `since` et acceptent `?since=<jeton>`.

## Listes

| Route | Qui | Corps |
|---|---|---|
| `GET /api/v1/works?limit=&offset=&since=` | tout profil connecté, session ou clé ; **la vue du lecteur** (un enfant ne reçoit que ce que son plafond laisse passer) | `{items: [{work_id, title, kind}], total, since, removed}` |
| `GET /api/v1/shelves?since=` | administrateur seulement | `{items: [{id, name, max_age_rating}], since, removed}` |

`since` est le jeton à envoyer à l'appel suivant. Sans `?since=`, `removed` est vide.

## Le jeton

- Opaque : `s2.<…>.<…>`. Le client ne le lit pas, ne le fabrique pas, ne le modifie pas, ne le compare pas.
- Adossé à la **séquence** de la table des événements, jamais à une heure.
- Signé par l'instance et propre à sa liste : un jeton de `/shelves` est refusé sur `/works`.
- Un jeton ne désigne pas son porteur : la réponse est celle du lecteur qui l'envoie.

## `removed`

Les identifiants sortis de la vue **de ce lecteur** depuis le jeton :

- les Œuvres supprimées ;
- les Œuvres qui ont cessé d'être visibles pour lui (une classification relevée, par exemple 18+ pour un enfant).

Limites, toutes testées : jamais un identifiant d'Œuvre que le lecteur ne voyait pas **au moment du jeton** ; jamais une Œuvre créée après le jeton ; jamais un titre. Un adulte voit tout, donc n'a que les suppressions.

## Erreurs

| Statut | Code | Quand | Que faire |
|---|---|---|---|
| 400 | `invalid_cursor` | jeton modifié, tronqué, d'une autre liste, de l'ancien format, trop long | ne pas réessayer ce jeton ; recharger la liste |
| 410 | `resync_required` | jeton plus ancien que la rétention des événements, jeton d'une instance restaurée en arrière, ou plus de 1000 retraits | recharger la liste (sans `since`), garder le `since` de cette réponse |
| 401 / 403 | | pas de session ; `/shelves` demandée par un non-administrateur | |

La rétention des événements est de 90 jours par défaut (`retention_events_days`) : un client qui ne se synchronise pas pendant plus longtemps recharge la liste.
