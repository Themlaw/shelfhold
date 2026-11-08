# Routes Kavita appelées (liste blanche)

Source unique des routes de l'API Kavita que Shelfhold appelle (EF-EXP-009, ENF-COMP-001,
ENF-SEC-013). L'adaptateur (`backend/src/shelfhold/export/targets/kavita/routes.py`) refuse toute
autre route avant d'émettre la requête ; un test hors ligne vérifie que ce tableau, le code et
l'`openapi.json` figé de chaque version de la plage disent la même chose
(`test_kavita_allowlisted_routes_exist_in_pinned_openapi`). Ajouter une route, c'est modifier ce
tableau, le code et les fixtures dans la même PR.

L'API de Kavita n'a ni préfixe de version ni politique de dépréciation : 26 routes ont disparu
entre 0.8.9.1 et 0.9.1.4. Un 404 sur l'une de ces routes met la destination en
`degraded_feature` (`target.route_missing`) ; une version hors plage la met en
`degraded_readonly` (`target.version_unsupported`).

**Authentification.** Clé nommée à expiration de l'utilisateur dédié (administrateur), envoyée
dans l'en-tête `x-api-key`, jamais dans l'URL. La clé est détenue par le cœur et n'est jamais
transmise à un plugin de destination ; seule `scan-folder` la reçoit aussi dans son corps
(`apiKey`, champ de `ScanFolderDto`), ajoutée par le cœur au moment de l'envoi.

| Méthode | Route | Usage | Jalon |
| --- | --- | --- | --- |
| GET | `/api/Server/server-info-slim` | Lire la version de Kavita et l'identifiant d'installation (plage, `known_bad_versions`, empreinte d'instance) | M1a |
| GET | `/api/Plugin/authkey-expires` | Relire la date d'expiration de la clé dédiée (`target.key_expiring` à J-14 et J-1) | M1a |
| GET | `/api/Plugin/parse` | Contrôle du **nom** d'un fichier avec le `libraryType` de la bibliothèque (collisions au plan, golden de nommage) | M1a |
| POST | `/api/Plugin/parse-bulk` | Même contrôle par lots de 100 noms au plus (adoption, rejeu après mise à jour de Kavita) | M1a |
| GET | `/api/Library/libraries` | Lire les bibliothèques (réglages complets avant une mise à jour fidèle, inventaire, type) | M1a |
| GET | `/api/Library/type` | Lire le type Kavita d'une bibliothèque à l'adoption | M1a |
| POST | `/api/Library/create` | Créer la bibliothèque d'une étagère, avec son type et ses `excludePatterns` | M1a |
| POST | `/api/Library/update` | Mise à jour fidèle : lecture, puis envoi du même document avec le seul changement voulu | M1a |
| POST | `/api/Library/scan-folder` | Scan ciblé d'un dossier (chemin traduit, débouncé) ; exige un compte administrateur | M1a |
| POST | `/api/Upload/series` | Remplacer et verrouiller la couverture d'une série (image en base64, sans préfixe `data:`) | M1a |
| GET | `/api/Users` | Audit d'accès en lecture seule : comptes, rôles, restriction d'âge, « inclure les inconnus », bibliothèques accessibles (EF-EXP-055) ; exige un compte administrateur | M1a |
| POST | `/api/Series/all-v2` | Lire les séries d'une bibliothèque (filtre `Libraries`, pages de 100) : confirmation d'arrivée, retrouver une série par son dossier ; une lecture malgré le `POST` | M1a |
| POST | `/api/Series/series-by-ids` | Relire le nom courant des séries mises en cache avant de conclure à une collision (EF-EXP-057) ; une lecture malgré le `POST` | M1a |
| GET | `/api/Series/series-detail` | Lister les fichiers indexés d'une série (confirmation d'arrivée du chapitre, EF-EXP-034) | M1a |
| GET | `/api/Series/metadata` | Lire l'âge de la série et son verrou (registre `field_owner`, âge dérivé des tags, EF-EXP-057) | M1a |
| POST | `/api/Series/metadata` | Pousser l'âge de série **verrouillé** (`ageRating`, `ageRatingLocked`) : le document lu est renvoyé tel quel avec ces deux seuls champs changés, car Kavita applique le document entier ; seulement pour un champ du registre `field_owner` ou libre, jamais pour un verrou d'un tiers (EF-EXP-040, EF-EXP-057) ; exige un compte administrateur | M1b |

## Routes de mise en place (hors du client d'exploitation)

`shelfhold target add kavita` (EF-INT-024) échange **une fois** le compte dédié contre une clé
nommée à expiration. Ces deux routes ne sont pas dans `ALLOWED_ROUTES` : le client d'exploitation
les refuse, seule la configuration sans interface les nomme (`SETUP_ROUTES`). Le mot de passe part
dans un corps JSON, jamais dans une URL, vers l'adresse déclarée seulement ; la clé par défaut du
compte (`apiKey` de la réponse de connexion) n'est ni lue ni gardée. Le jeton de connexion n'est
utilisé que pour la seconde requête : l'OpenAPI de Kavita n'a pas de route de déconnexion, il
expire de lui-même. Si l'enregistrement en base échoue après la création de la clé, celle-ci reste
chez Kavita : le message d'erreur donne son nom et son identifiant, à révoquer dans Kavita.

| Méthode | Route | Usage | Jalon |
| --- | --- | --- | --- |
| POST | `/api/Account/login` | Connexion du compte dédié (`LoginDto`), pour obtenir un jeton | M1a |
| POST | `/api/Account/create-auth-key` | Créer la clé nommée à expiration (`RotateAuthKeyRequestDto` : `name`, `keyLength` 8 à 32, `expiresUtc`) | M1a |
| GET | `/api/Account/auth-keys` | Lister les clés du compte (`AuthKeyDto` : `id`, `name`, `key`), à la rotation, pour retrouver l'ancienne | M1a |
| DELETE | `/api/Account/auth-key` | Révoquer l'ancienne clé après la rotation (`authKeyId` en requête) | M1a |

## Fixtures `openapi.json`

`backend/tests/fixtures/kavita/openapi/{0.9.1,pinned,latest}.json` : le `openapi.json` publié à la
racine du dépôt `Kareadita/Kavita` au tag de chaque version, réduit aux routes ci-dessus (et aux
schémas qu'elles citent). Origine, tags et commits : `SOURCES.md` du même dossier. Récupération
unique, hors CI ; la CI reste hors ligne. Pour les rafraîchir : `tools/reduce_kavita_openapi.py`.
