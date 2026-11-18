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
  compte n'est pas conservé. Chaque acceptation est **journalisée** (table `audit`, action
  `target.admin_risk_accepted`, avec le nom de la destination et l'utilisateur de l'hôte ou la
  session qui l'a donnée).
- Options de `target add kavita` : `--name` (défaut `kavita`) et `--path-map` (le même JSON que la clé
  `path_map.<cible>`, donné d'emblée plutôt que par `config set` ensuite). Options de `shelf create` :
  `--subpath` et `--phrase` (libellé affiché, défaut : le nom). Fichiers de secrets : voir
  [installation-headless.md](installation-headless.md) (conteneur en lecture seule, entrée standard `-`).
- `target add komga` et `target add cwa` (M4 L46) :
  - **Komga** : `shelfhold target add komga --url http://komga:25600 --password-file ./komga.key`. Le fichier
    (ou `-` pour l'entrée standard, jamais un argument) contient la **clé d'API d'un compte administrateur
    dédié** de Komga (le scan et `empty-trash` exigent ce rôle ; `--api-key-file` est un synonyme). La clé est
    montrée une fois à Komga (`/actuator/info`, ce qui prouve le rôle), **scellée** dans la ligne de sa
    destination et **jamais relue** : ni une réponse de l'API, ni l'export intégral, ni un journal ne la portent.
    Une clé refusée ou une adresse hors de portée (adresse privée qu'on n'a pas déclarée comprise) n'enregistre rien.
    Options : `--name` (défaut `komga`), `--path-map`. Chaque création est journalisée (`target.add_komga`).
  - **CWA** : `shelfhold target add cwa --name depot --path-map '[{"local": "/library/depot", "remote":
    "/cwa-ingest", "mode": "rw"}]'`. C'est le dossier que Calibre-Web Automated surveille : pas de secret, des dossiers
    en écriture seulement, jamais la source locale de Suwayomi. Journalisé (`target.add_cwa`).
  - HTTP : `POST /api/v1/targets/komga` (`name`, `url`, `api_key`, `path_map`) et `POST /api/v1/targets/cwa`
    (`name`, `path_map`). La première est une action réservée (`target.add_komga`), la seconde l'est comme la
    création d'un dossier natif (`config.path_map`) : session d'administrateur fraîche, jamais une clé, un membre ou un enfant.
- `target sync-progress <liaison> --on|--off` (M4 L46) allume ou éteint la **synchro de progression** d'une
  liaison (`shelf_destination.sync_progress`, lue par la passe `progress.sync`). Même opération en HTTP :
  `POST /api/v1/destinations/{id}/sync-progress` avec `{"enabled": true|false}`, et la case de l'écran des
  destinations. Action réservée (`destination.sync_progress`) : session fraîche d'administrateur ou ligne de commande
  de l'hôte, jamais une clé, un membre ou un enfant. Chaque changement est journalisé (`<liaison> on|off`),
  une répétition ne l'est pas. Seule une destination qui garde une position de lecture par compte (Kavita) peut
  l'allumer : Komga, une boîte de dépôt CWA et un dossier natif répondent `destination.sync_unsupported` (409) ;
  éteindre est toujours permis.
- La clé est créée par `/api/Account/create-auth-key` ; si Shelfhold ne peut pas l'enregistrer, l'erreur donne son nom et son identifiant : révoquez-la dans Kavita.
- L'adresse d'une destination : `http://` seulement sur le réseau local (adresse privée, nom sans point, `.local`, `.lan`, `.internal`), sinon `https://` ; une IP publique est refusée.
- `source add suwayomi` lit le mode d'authentification du serveur (`none`, `basic_auth` ou
  `ui_login`) ; tout autre mode est refusé (`source.auth_mode_unsupported`). Le secret est
  `utilisateur:mot_de_passe`, une ligne. En `ui_login` (Suwayomi multi-comptes), c'est celui du
  **compte de service** de Shelfhold : créez-lui un compte dédié aux droits minimaux dès que votre
  Suwayomi le permet (jamais l'installation d'extensions ; un compte que le serveur dit pouvoir en
  installer est refusé). Shelfhold ne lit que la bibliothèque de ce compte, jamais celle des autres :
  la famille demande ses séries par Shelfhold. Si le serveur n'expose pas les rôles du compte (c'est le cas de Suwayomi v2.4.2366), Shelfhold ne peut pas vérifier ses droits : `shelfhold status` affiche `roles_unverified` jusqu'à `shelfhold source confirm-roles` (audité). Les jetons de connexion ne sont gardés qu'en mémoire.
- `shelf create` : seules les étagères **18+** ou **X** existent en M1a. Si Shelfhold ne peut pas écrire dans le dossier de l'étagère, la commande s'arrête (rien n'est créé) et donne la cause (`EACCES`, propriétaire, mode, uid d'exécution), la commande `setfacl` à lancer sur l'hôte et le bloc `compose` exact (`user:` ou `group_add:`) à coller (EF-UX-002).
- Les commandes sont idempotentes : les relancer ne change rien.

## `config get|set|list`

| Clé | Défaut | Rôle |
| --- | --- | --- |
| `public_url` | (aucun) | Adresse publique ; toute URL absolue en est tirée, jamais de l'en-tête `Host` (ENF-SEC-016). |
| `source.check_mode` | `always_on` | `always_on` ou `piloted` (EF-SRC-025). `piloted` : Shelfhold réveille Suwayomi (si un réveil est configuré) puis lui demande de rafraîchir les séries suivies avant de lire les nouveautés. Sans valeur enregistrée, le défaut est `piloted` quand un réveil Sablier est configuré, `always_on` sinon ([reveil-sablier.md](reveil-sablier.md)). |
| `source.read_state_owner` | vide : l'administrateur | Le profil dont le « lu » est renvoyé à Suwayomi (EF-SRC-026) : un nom d'utilisateur. Suwayomi n'a qu'un utilisateur : un seul profil parle pour lui. Un enfant n'est jamais renvoyé ; un réglage qui en désigne un arrête l'envoi. Voir [read-state-push.md](../reference/read-state-push.md). |
| `export.missing_floor` | `10` | Sentinelle (EF-EXP-029) : nombre minimal de fichiers manquants dans une destination d'étagère pour que la part compte. |
| `export.missing_ratio` | `0.05` | Sentinelle : part de fichiers manquants (avec le plancher) à partir de laquelle vérification, suppressions et scans sont bloqués. |
| `export.missing_ceiling` | `50` | Sentinelle : au-delà de ce nombre de fichiers manquants, blocage quelle que soit la part. Bornes : part dans ]0 ; 0,5], plafond au plus 1000, plancher entre 1 et le plafond (sinon le garde-fou ne pourrait plus se déclencher) ; une ligne modifiée à la main hors bornes est ignorée avec un WARNING. Lus à chaque passe, sans redémarrage. |
| `maintenance.window` | `03:00-07:00 Europe/Paris` | Fixe en M1a, lecture seule (ENF-OPS-014). |
| `instance.jurisdiction` | déduite de la langue du système | Code pays à deux lettres (par ex. `FR`) dont le droit d'auteur conduit les mentions de domaine public (EF-SRC-024). |
| `notify.urls` | (aucun) | `nom=url`, scellée ; fournie par `--value-file`, jamais en argument ([notifications.md](notifications.md)). |
| `path_map.<cible>` | `/library/manga↔/manga`, `/library/books↔/books`, `/library/comics↔/comics` | Correspondance des chemins de la destination (JSON). Sans `--path-map`, `target add kavita` pose cette table par défaut (EF-EXP-003) : on monte le parent des médias de Kavita ; seuls les dossiers présents deviennent des racines, et un dossier déjà nommé par une autre destination ou déjà enregistré est sauté. `--path-map '[]'` pose une table vide. Déclarer `adopt_ro` un dossier déjà racine en écriture est refusé. |

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
