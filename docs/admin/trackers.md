# Trackers de lecture : lier un profil à un service distant

Un **tracker** est un plugin tiers qui relie le profil d'un membre à son compte sur un service de
suivi de lecture (Goodreads, AniList, un service personnel…). Le lien est **par profil** : personne
ne lie le tracker d'un autre, l'administrateur compris. Le jeton du service est remis **une fois**,
puis scellé ; les réponses ne portent que son empreinte.

## Routes

- `GET /api/v1/profiles/{user_id}/trackers`
- `GET /api/v1/profiles/{user_id}/trackers/{plugin_key}`
- `PUT /api/v1/profiles/{user_id}/trackers/{plugin_key}`
- `DELETE /api/v1/profiles/{user_id}/trackers/{plugin_key}`
- `PUT /api/v1/profiles/{user_id}/trackers/{plugin_key}/push`
- `POST /api/v1/profiles/{user_id}/trackers/{plugin_key}/progress`
- `POST /api/v1/profiles/{user_id}/trackers/{plugin_key}/pull`
- `PUT /api/v1/profiles/{user_id}/trackers/{plugin_key}/allowance`

## Lier un tracker
```sh
# par l'API, en session du profil concerné
PUT /api/v1/profiles/{user_id}/trackers/{plugin_key}
{ "token": "…" }
```

Lier est une action réservée (`tracker.link`) : une session fraîche du profil, jamais une clé
d'API. Le jeton est dans le corps de la requête une seule fois et nulle part ailleurs.

- `GET /api/v1/profiles/{user_id}/trackers` : la liste des liens (sans empreinte de jeton).
- `GET /api/v1/profiles/{user_id}/trackers/{plugin_key}` : un lien, avec son empreinte.
- `DELETE /api/v1/profiles/{user_id}/trackers/{plugin_key}` : délier (`tracker.unlink`).

## Envoi de la progression (opt-in)

Rien ne part sans accord explicite du profil :

```sh
PUT /api/v1/profiles/{user_id}/trackers/{plugin_key}/push
{ "enabled": true }
```

`tracker.push_opt_in` est une action réservée. Tant qu'elle n'est pas donnée, le tracker n'envoie
rien (`tracker_push_refused` si on tente).

```sh
POST /api/v1/profiles/{user_id}/trackers/{plugin_key}/progress
{ "fraction": 0.42, "at": "2026-…" }
```

L'envoi est fait par le cœur, avec sa garde anti-SSRF, la liste de domaines du manifeste et la
rédaction des journaux. Aucun secret ne traverse vers le plugin isolé.

## Tirer une indication (pull)

```sh
POST /api/v1/profiles/{user_id}/trackers/{plugin_key}/pull
```

La réponse est une **indication** (`fraction`, `at`, `hint: true`) : le profil est là-bas à telle
avancée. Elle n'écrit rien : jamais un marque-page, jamais une progression. C'est une aide pour le
lecteur, pas un marquage de lecture.

## Profils enfants

Un profil enfant ne lie un tracker que là où l'administrateur l'a autorisé :

```sh
PUT /api/v1/profiles/{child_id}/trackers/{plugin_key}/allowance
{ "allowed": true }
```

`tracker.allow_child` est une action réservée de l'administrateur, depuis sa propre session. Sans
cela, la liaison répond `tracker_child_refused`.

## Codes d'erreur

- `tracker_not_found` : le plugin n'est pas là, aucun lien n'existe, ou il n'est pas à vous (un
  tracker d'un autre profil répond comme un tracker absent).
- `tracker_child_refused` : tracker non permis à un profil enfant.
- `tracker_push_refused` : l'envoi n'est pas activé.
- `tracker_refused` : le jeton est refusé, le plugin ne déclare aucun moyen de le porter, ou le
  service distant n'a pas répondu (la raison est dans l'extension `reason`).

Voir aussi [plugins-tiers.md](plugins-tiers.md), [../plugins/kit.md](../plugins/kit.md).