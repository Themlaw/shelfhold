# Configuration sans interface (EF-INT-024)

De l'installation au premier chapitre dans Kavita, sans interface web et sans écrire de SQL. Les
commandes passent par la socket du serveur quand il tourne, sinon la commande prend elle-même le
verrou de `/data` (voir `docs/configuration.md`).

Un secret (mot de passe, clé) n'est **jamais** un argument de ligne de commande : `argv` est visible
dans `ps`. Il se lit dans un fichier (`chmod 600`) ou sur l'entrée standard (`-`).

## Pas à pas

```sh
shelfhold admin create --username admin --password-file ./admin.pw
shelfhold config set public_url https://shelfhold.example.org
shelfhold target add kavita --url http://kavita:5000 --user shelfhold \
    --password-file ./kavita.pw --accept-admin-risk
shelfhold config set path_map.kavita '[{"local": "/library/manga", "remote": "/manga"}]'
shelfhold source add suwayomi --url http://suwayomi:4567 --secret-file ./suwayomi.secret
shelfhold shelf create Manga --max-age 18+ --target kavita --subpath manga
```

- `target add kavita` échange le compte contre une **clé nommée à expiration** (90 jours) ; la clé
  est celle d'un compte **administrateur** de Kavita et donne tout pouvoir sur lui : la commande
  s'arrête sur cet avertissement tant que `--accept-admin-risk` n'est pas donné. Le mot de passe du
  compte n'est pas conservé.
- Options de `target add kavita` : `--name` (défaut `kavita`) et `--path-map` (le même JSON que la clé
  `path_map.<cible>`, donné d'emblée plutôt que par `config set` ensuite). Options de `shelf create` :
  `--subpath` et `--phrase` (libellé affiché, défaut : le nom). Fichiers de secrets : voir
  [installation-headless.md](installation-headless.md) (conteneur en lecture seule, entrée standard `-`).
- La clé est créée par `/api/Account/create-auth-key` ; si Shelfhold ne peut pas l'enregistrer, l'erreur donne son nom et son identifiant : révoquez-la dans Kavita.
- L'adresse d'une destination : `http://` seulement sur le réseau local (adresse privée, nom sans point, `.local`, `.lan`, `.internal`), sinon `https://` ; une IP publique est refusée.
- `source add suwayomi` lit le mode d'authentification du serveur (`none` ou `basic_auth`) ; tout
  autre mode (`ui_login`) est refusé. Le secret est `utilisateur:mot_de_passe`, une ligne.
- `shelf create` : seules les étagères **18+** ou **X** existent en M1a.
- Les commandes sont idempotentes : les relancer ne change rien.

## `config get|set|list`

| Clé | Défaut | Rôle |
| --- | --- | --- |
| `public_url` | (aucun) | Adresse publique ; toute URL absolue en est tirée, jamais de l'en-tête `Host` (ENF-SEC-016). |
| `source.check_mode` | `always_on` | `always_on` ou `piloted` (EF-SRC-025). |
| `maintenance.window` | `03:00-07:00 Europe/Paris` | Fixe en M1a, lecture seule (ENF-OPS-014). |
| `instance.jurisdiction` | déduite de la langue du système | Code pays à deux lettres (par ex. `FR`) dont le droit d'auteur conduit les mentions de domaine public (EF-SRC-024). |
| `notify.urls` | (aucun) | `nom=url`, scellée ; fournie par `--value-file`, jamais en argument ([notifications.md](notifications.md)). |
| `path_map.<cible>` | `[]` | Correspondance des chemins de la destination (JSON). |

`public_url` n'est lu qu'au démarrage : `shelfhold status` signale qu'un redémarrage est nécessaire. L'environnement et `config.toml` l'emportent sur la base (ENF-OPS-004) : `config set` le signale.

## `config apply -f shelfhold.toml`

Un fichier déclaratif (nom libre, distinct de `config.toml`), mêmes clés, idempotent.
`--dry-run` affiche les différences sans rien écrire, sonder ni échanger.

```toml
schema_version = "1.0"
public_url = "https://shelfhold.example.org"

[source]
check_mode = "always_on"

[[targets]]
kind = "kavita"
name = "kavita"
url = "http://kavita:5000"
user = "shelfhold"
password_file = "kavita.pw"          # un chemin, jamais le mot de passe
accept_admin_risk = true
path_map = [{ local = "/library/manga", remote = "/manga" }]

[[sources]]
kind = "suwayomi"
url = "http://suwayomi:4567"
secret_file = "suwayomi.secret"

[[shelves]]
name = "Manga"
max_age = "18+"
target = "kavita"
subpath = "manga"

[notify.urls]                        # facultatif ; l'URL porte un jeton : fichier en chmod 600
alertes = "discord://identifiant/jeton"
```

`apply` n'est pas atomique : le plan est validé en entier d'abord, puis exécuté étape par étape ; en cas d'échec, relancez (reprise idempotente). Un secret ou un compte qui change apparaît comme `[secret: changed]`, sans la valeur. Une clé inconnue est une erreur. Les chemins relatifs sont relatifs au dossier du fichier. Les
secrets sont lus par la commande et envoyés au serveur par la socket (0600), jamais affichés.

## Routes

`GET|PATCH /api/v1/config`, `POST /api/v1/targets`, `POST /api/v1/sources`,
`POST /api/v1/shelves`. Créer une destination Kavita est l'action réservée `admin_risk.accept` :
session administrateur **fraîche (15 min), quel que soit le profil**, jamais une clé d'API.
`PATCH /api/v1/config` sur `public_url` ou `path_map.*` exige aussi une session. Le fichier `open-api/openapi.json` ne décrit pas encore `/config`, `/targets` ni `/sources`, ni les routes d'exploitation de [etats-attente-et-sorties.md](etats-attente-et-sorties.md) : la ligne de commande et ce texte font foi. Chaque `Location` est construit depuis
`public_url` ; sans elle, l'en-tête est absent.
