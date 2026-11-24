# Index de plugins (M6)

Un plugin tiers n'arrive pas jusqu'à un foyer par la magie : il est **publié** (sur PyPI ou sous
forme de roue), puis **référencé** par un **index signé**, que l'administrateur épingle à son
signataire. Cette page décrit le chemin de l'auteur et le contrat de l'index. Le chemin de
l'hébergeur est dans [heberger-un-index.md](heberger-un-index.md) ; le cycle de vie d'un plugin
installé dans [cycle-de-vie.md](cycle-de-vie.md) ; le mode isolé dans [isolation.md](isolation.md).

## 1. Publier la roue

- Construisez une roue Python pure (pas de bytecode compilé, pas de `.so`, pas de `.pth` :
  voir [../admin/plugins-tiers.md](../admin/plugins-tiers.md)).
- Déclarez la dépendance au SDK publié : `shelfhold-sdk>=2.0,<3` (distribution MIT, import
  `shelfhold_sdk`). Ne dépendez jamais de l'intérieur de Shelfhold (ENF-LEG-010).
- Publiez sur PyPI par **publication de confiance (OIDC)**, sans jeton longue durée, ou
  conservez la roue et servez-la vous-même : un index pointe vers une `wheel_url` (HTTPS recommandé ; HTTP est accepté, car la signature de l'index et le `sha256` de la roue en garantissent l'intégrité).
- Le manifeste déclare `api = 2` (ou `1` pendant la fenêtre du shim, jusqu'en 2027-11) et
  `rpc_protocol = 1`.

## 2. Demander l'inscription à l'index officiel

L'index officiel est une liste signée, sans dépôt de code : il ne contient que des références
(une clé de plugin, une version, une adresse de roue, une empreinte). Pour y entrer :

1. La roue passe le kit de conformité (`shelfhold_sdk.testing`), hors réseau, sur des cassettes
   synthétiques, **y compris en mode isolé** (`isolated=True`).
2. Preuve de licence ou d'autorisation, lecture des conditions et du fichier robots de la
   source amont, limites déclarées (`rate_limit`, `quota`), aucun secret, revue par les
   mainteneurs (CODEOWNERS) — liste d'admission ENF-LEG-003.
3. Une pull request sur le dépôt de l'index, avec la ligne `Signed-off-by` (DCO), portant la
   nouvelle entrée et la nouvelle version du document.
4. L'index est **resigné** par le mainteneur : la version monte, la date d'expiration est
   posée (jamais à plus de 90 jours).

Aucune liste de sites n'est tenue dans le dépôt de Shelfhold : l'index est une liste de
plugins, pas de sources amont. Une source de fictions en ligne ne vit que comme plugin tiers,
hors dépôt, sans nom de site ni d'outil dans le dépôt (AB4).

## 3. Ce qu'un index contient

Document v1, JSON strict (champs inconnus refusés, clé en double refusée) :

| Champ du document | Sens |
| --- | --- |
| `format` | `1` |
| `version` | entier monotone ; un index plus ancien est refusé |
| `expires` | instant ISO 8601 UTC, à au plus 90 jours |
| `plugins` | liste d'entrées (voir ci-dessous) |
| `revoked` | `plugin_keys` et `wheel_sha256` : révocations qui restent vues, même omises plus tard |
| `next_pubkey` | présent seulement pendant une rotation |

Chaque entrée :

| Champ | Sens |
| --- | --- |
| `key` | clé stable du plugin (motif `KEY`) |
| `version` | version sémantique de la roue |
| `api` | numéro du contrat de données (`2` ou `1` par le shim) |
| `rpc_protocol` | version du protocole fil (`1`) |
| `wheel_url` | adresse de la roue (`http` ou `https`, HTTPS recommandé) |
| `sha256` | `sha256:` + 64 hexadécimaux ; la roue est vérifiée contre cette valeur |
| `min_host` | version minimale de Shelfhold (dotted) |
| `deprecated` | booléen, lu par l'administrateur, jamais agi par l'hôte |
| `replaced_by` | clé du remplaçant nommé par l'index, ou `null` |

## 4. Signature : minisign

L'index est signé avec **minisign**, au format **préhaché** (Ed25519 sur le condensat BLAKE2b-512
du document) :

- signature détachée dans `index.json.minisig` (quatre lignes) ;
- **commentaire de confiance** signé, qui doit redire la `version` et l'`expires` du document ;
- l'ancien format non préhaché (`Ed`) est refusé, quel que soit le signataire ;
- la clé publique est le blob base64 d'un fichier de clé minisign (`Ed` + identifiant de clé +
  clé brute) ; son identifiant de clé (16 hexadécimaux) est l'**empreinte** que l'administrateur
  vérifie et que la signature doit répéter.

Aucune clé ne livre avec le produit. L'administrateur épingle un dépôt par son adresse **et** par
la clé publique de son signataire ; sans la clé, il n'y a pas d'épingle (action réservée).

## 5. Rotation de clé

Pendant une rotation, le document porte `next_pubkey`. L'épingle ne bouge que sur une **double
signature** : le document signé par la clé épinglée **et** par la clé suivante, avec le même
commentaire de confiance. L'administrateur n'a rien à faire : le rafraîchissement déplace
l'épingle. Les révocations déjà vues restent vues.

## 6. Cycle de vie côté hôte

Voir [cycle-de-vie.md](cycle-de-vie.md). En bref :

- `min_host` au-dessus de l'hôte : refus à l'installation comme à la mise à jour (`min_host`).
- `api` hors de la fenêtre N / N-1 : plugin marqué `incompatible`, événement `plugin.incompatible`
  une seule fois par couple (plugin, raison).
- `deprecated` et `replaced_by` sont lus par l'administrateur ; l'hôte n'installe jamais le
  remplaçant de son propre chef.
- `migrate_state(from_version, key, payload)` est obligatoire quand `payload_schema` change ;
  le kit de conformité l'exige et l'hôte l'appelle avant le premier appel de la nouvelle version.
- Le retour arrière vers une version qui ne sait pas lire l'état écrit est refusé
  (`state_newer`).
- Le mode isolé est recommandé (voir [isolation.md](isolation.md)).

## 7. Licence

Le dépôt de Shelfhold est sous AGPL-3.0-or-later, avec une **exception de plugin provisoire**
(ENF-LEG-010) qui ne couvre que l'API publique du SDK (`shelfhold_sdk`, MIT). Tant qu'elle n'est
pas relue, publiez votre plugin sous une licence **compatible AGPL**. Copiez l'en-tête de
[gabarit/NOTICE-LICENCE.md](gabarit/NOTICE-LICENCE.md).
