# Cycle de vie d'un plugin (M6)

Un plugin installé vit dans le temps : l'hôte monte de version, l'auteur publie une nouvelle
roue, l'état persistant grandit. Cette page rassemble les règles qui évitent qu'un plugin
devienne un poids mort ou un danger.

## `deprecated` et `replaced_by`

- `deprecated` (booléen de l'entrée d'index) est une **mention** : le plugin reste installable
  et mis à jour comme les autres. L'hôte ne l'agite jamais.
- `replaced_by` nomme une **autre clé** que l'index propose comme remplaçant. Ce n'est pas une
  installation automatique : le remplacement est un geste à part, avec ses propres vérifications.
- `shelfhold plugin migrate-to <clé> <remplaçant>` (action réservée, auditée) déplace les
  sources de l'ancien plugin vers le nouveau, en consultant les pierres tombales : une série
  effacée ne revient pas, et la ligne qui la nommerait, sous l'une ou l'autre forme, reste
  bloquée.

## `min_host`

L'entrée d'index porte `min_host`, la version minimale de Shelfhold pour laquelle la roue est
faite. Si `min_host` est au-dessus de l'hôte :

- refus à l'installation et à la mise à jour, code `min_host` ;
- après une montée de Shelfhold, un plugin devenu incompatible est marqué et jamais chargé.

## `api` et `plugin.incompatible`

L'hôte accepte `api` N nativement et `api` N-1 par un shim pendant 12 mois (voir
[compatibilite.md](compatibilite.md)). Au-delà, ou si `min_host` dépasse l'hôte, le plugin est
marqué `incompatible` et l'événement `plugin.incompatible` est émis **une seule fois** par
couple (plugin, raison). Redémarrer ne répète pas l'événement. Les autres plugins continuent
de charger.

## ABI de la roue

Au démarrage, les étiquettes PEP 425 de la roue installée sont comparées à l'interpréteur.
Une roue qui ne correspond plus (par exemple `cp312` sur un hôte 3.13) n'est **jamais chargée**,
quel que soit son mode, et `shelfhold plugin list` l'indique (« ABI incompatible ») avec la
réinstallation guidée.

La politique de montée est « Python N+1 » : l'hôte passe à la version suivante un an avant la
fin de vie de la version courante, pour laisser le temps aux auteurs de publier une roue
compatible (voir [../admin/python-et-plugins.md](../admin/python-et-plugins.md)).

## `migrate_state` et état persistant

L'état persistant d'un plugin est versionné par `payload_schema` (1 par défaut). Une version
qui change le schéma embarque :

```python
def migrate_state(self, from_version: int, key: str, payload: Any) -> Any:
    ...
```

- le kit de conformité réclame `migrate_state(from_version, key, payload)` dès que
  `payload_schema` diffère de la version précédente (`state_migration_declared`) ;
- l'hôte appelle le crochet avant le premier appel de la nouvelle version : chaque document
  écrit sous un schéma plus ancien est remis au crochet et réécrit sous le nouveau, estampillé
  par la version qui a migré ;
- le crochet est le code du plugin, donc ses erreurs sont des refus typés (`hook_failed`), pas
  des plantages de l'hôte ; un crochet qui répond `None` ou une valeur non sérialisable est
  refusé ;
- un schéma qui change sans crochet est refusé (`migration_missing`).

## Retour arrière refusé (`state_newer`)

Le retour arrière (`shelfhold plugin rollback`) vers une version qui ne sait pas lire l'état
écrit est refusé avec la raison `state_newer` : une version qui n'a pas écrit l'état ne peut
pas être sûre de le lire. L'état n'est jamais corrompu par un code qui ne le comprend pas, et
le plugin installé est laissé exactement tel qu'il était.

## Mise à jour, épingle, désinstallation

Voir [../admin/plugins-tiers.md](../admin/plugins-tiers.md) et L09a/L09b. En bref :

- une mise à jour depuis un index vérifie la roue par son `sha256` et la garde pour le retour
  arrière ;
- un plugin désinstallé laisse ses liaisons orphelines signalées, sans perte de contenu ;
- une clé déjà installée n'est jamais écrasée par un autre signataire : c'est une installation
  distincte, et les données de l'ancien signataire ne sont déplacées qu'après confirmation.

## Mode isolé recommandé

Le mode par défaut est **isolé** (`isolated`), dans le compagnon `plugin-runner` (voir
[isolation.md](isolation.md)). Le mode en processus (`in_process`) est un choix explicite de
l'administrateur, derrière une variable d'environnement et une confirmation tapée liée à
l'empreinte des fichiers. Un plugin ne peut pas demander son mode dans son manifeste.

## Licence (D-P4)

L'exception de plugin est **provisoire**. Tant qu'elle n'est pas relue, préférez une licence
compatible AGPL et ne dépendez d'aucune API interne. Voir la page 5 de
[parcours.md](parcours.md).
