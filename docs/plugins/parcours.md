# Parcours de l'auteur de plugin

Cette page dit dans quel ordre faire les choses pour écrire une **source** ou une **destination** pour
Shelfhold, et ce que le projet promet en retour. Elle renvoie aux pages de détail ; chaque affirmation sur
le SDK a été relu contre le code (`sdk/src/shelfhold_sdk/`, version **SDK 2.0**).

Ce qui existe aujourd'hui et ce qui est à venir :

| Étape | Sources | Destinations |
| --- | --- | --- |
| Contrat | stable, SDK 2.0 ([source.md](source.md), [compatibilite.md](compatibilite.md)) | contrat 2.0 : plan `FsOp` et `ApiCall.body` figés, le reste **provisoire** ([destination.md](destination.md)) ; `api` 1 accepté par le shim jusqu'en 2027-11 |
| Kit de conformité | livré ([kit.md](kit.md)) | à venir en M4 : seuls le contrôle du plan d'opérations et celui des déclarations existent |
| Index officiel, hébergement d'un index tiers, migrations d'état | livré en M6 ([index.md](index.md), [heberger-un-index.md](heberger-un-index.md), [cycle-de-vie.md](cycle-de-vie.md)) | livré en M6 |

Un plugin tiers est **désactivé par défaut** et tourne avec les droits de Shelfhold : lisez
[../admin/plugins-tiers.md](../admin/plugins-tiers.md) avant d'en publier un.

## 1. Partir du gabarit

Le dossier [gabarit/](gabarit/) est un dépôt de plugin minimal : une source d'exemple, sa cassette
synthétique, un test qui lance le kit, la note de licence. Le test de ce dépôt exécute le gabarit : si le
gabarit cesse de passer le kit, la CI le dit. Copiez-le, changez la clé, l'hôte et le manifeste, gardez la
forme.

## 2. Écrire le manifeste, puis les méthodes

- Une source : [source.md](source.md). Un fournisseur de métadonnées : [metadata-provider.md](metadata-provider.md).
- Une destination : [destination.md](destination.md). Elle ne touche jamais un fichier : elle **décrit** un plan
  d'opérations (`FsOp`) que l'hôte valide et exécute.
- Le plugin n'ouvre jamais lui-même un socket, un fichier ni un processus : tout passe par le `Fetcher` et par le
  plan. Un détecteur d'entrées-sorties directes le refuse et fait échouer la conformité.

## 3. Passer le kit de conformité

[kit.md](kit.md) : cassettes synthétiques, aucun réseau possible, aucune écriture hors du plan. Un test rouge
du kit est un défaut du plugin, pas du kit.

## 4. La règle de stabilité (EF-SRC-036)

Pour une source et pour un service connecté, `remote_unit_id` et `SeriesRef` **doivent rester stables** :

- à la **réinstallation** de l'outil connecté (base recréée, sauvegarde restaurée, renumérotation) ;
- à la rotation des identifiants.

`remote_unit_id` est l'identifiant de la **source amont**, jamais l'entier de la base de l'outil que vous
interrogez ; ce dernier n'est qu'un détail de transport, ré-résolu quand il manque. Sans cette règle, une
réinstallation créerait de nouvelles Variantes en double. Le kit la contrôle par une **réinstallation
simulée** (`simulated_reinstall`) : un service connecté **doit** fournir la fabrique qui recrée le plugin.

## 5. La licence

Le dépôt du projet est sous AGPL-3.0-or-later, avec une **exception de plugin provisoire** (ENF-LEG-010) :
elle couvre l'utilisation de l'API publique du SDK (`shelfhold_sdk`, sous licence MIT) et rien d'interne. Son
texte est **provisoire** : il sera relu avant la 1.0 et avant l'ouverture aux tiers de M6. Tant qu'il ne l'est
pas, publiez votre plugin sous une licence **compatible AGPL**. Ne dépendez d'aucune API interne.
[gabarit/NOTICE-LICENCE.md](gabarit/NOTICE-LICENCE.md) donne l'en-tête à copier.

## 6. La liste d'admission

Une source qui vise l'index officiel passe la liste d'admission de [source.md](source.md#liste-dadmission-enf-leg-003)
(preuve de licence ou d'autorisation, conditions et robots lus, limites déclarées, aucun secret, kit vert,
revue CODEOWNERS). Une destination passe par le même index signé qu'une source (voir [index.md](index.md)) et le même
mode isolé ([isolation.md](isolation.md)).

## 7. Publier et proposer le plugin

1. Publiez la roue sur PyPI (publication de confiance OIDC) ou servez-la vous-même, avec
   `shelfhold-sdk>=2.0,<3` en dépendance.
2. Demandez l'inscription à l'index officiel (voir [index.md](index.md)) ou hébergez votre propre
   index (voir [heberger-un-index.md](heberger-un-index.md)).
3. Ouvrez une PR avec le gabarit de PR du dépôt (`.github/pull_request_template.md`). Les commits
   portent une ligne `Signed-off-by` (DCO).

Un plugin reste **désactivé par défaut** tant que l'administrateur ne l'a pas installé et activé.
Le mode isolé est recommandé ([isolation.md](isolation.md)).

## Démos et captures

Toute démo ou capture d'écran n'utilise que des contenus libres. Aucun nom de site n'apparaît dans un
exemple, un test ou une cassette : utilisez un hôte réservé comme `catalogue.exemple.invalid`.
