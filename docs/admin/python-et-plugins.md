# Python et plugins : politique de l'hôte

Shelfhold embarque ses propres adaptateurs, mais un plugin tiers apporte son code, compilé ou
non pour l'interpréteur qui le chargera. Cette page dit quand l'hôte change de Python, ce que
l'hôte vérifie au démarrage, et ce que l'administrateur voit quand un plugin ne suit plus.

## La politique « Python N+1 »

L'hôte passe à la version N+1 de Python **un an avant la fin de vie de la version N**, pas le
mois de son retrait : les plugins ont une année pour publier une roue compatible, et
l'administrateur a le temps de la voir venir. Concrètement, l'image de Shelfhold annonce sa
prochaine montée de version dans ses notes ; une montée n'est jamais silencieuse.

## Ce que l'hôte vérifie au démarrage

Deux contrôles, à chaque démarrage, avant qu'un plugin ne soit chargé :

- **L'ABI de la roue installée.** Les étiquettes (tags PEP 425) du fichier `WHEEL` de la roue
  installée sont comparées à l'interpréteur qui tourne. Une roue `py3-none-any` passe partout ;
  une roue `abi3` passe depuis le Python pour lequel elle a été construite ; toute autre roue
  doit être celle de cet interpréteur précis. Une roue qui ne correspond plus — l'hôte est
  passé de Python 3.12 à 3.13, la roue est `cp312` — n'est **jamais chargée**, quel que soit son
  mode, et `shelfhold plugin list` le dit : « ABI incompatible », avec la réinstallation guidée
  (`shelfhold plugin install`).
- **Les champs de l'index.** L'entrée d'index qui a installé le plugin a été enregistrée avec
  ses exigences : `min_host` (la version minimale de l'hôte) et `api` (le numéro du contrat de
  données, l'hôte acceptant N et N-1). Après une montée de Shelfhold, un plugin devenu
  incompatible est marqué, jamais chargé, et l'événement `plugin.incompatible` est émis **une
  seule fois** par couple (plugin, raison) — redémarrer ne le répète pas.

## Ce que fait l'administrateur

1. `shelfhold plugin list` : la raison est écrite sur chaque ligne (ABI incompatible,
   `min_host`, `api`).
2. Mettre à jour le plugin depuis son dépôt d'index (`shelfhold plugin update <clé>`) : le
   dépôt sert la roue qui correspond à l'hôte actuel. Une entrée `min_host` supérieure à
   l'hôte est refusée à l'installation comme à la mise à jour, avec le code `min_host`.
3. Si le dépôt nomme un remplacement (`replaced_by`), l'installation du remplacement est un
   geste à part (une autre clé, ses propres vérifications) ; `shelfhold plugin migrate-to
   <clé> <remplacement>` (action réservée, auditée) déplace les sources de l'ancien plugin
   vers le nouveau, en consultant les pierres tombales : une série effacée ne revient pas, et
   la ligne qui la nommerait, sous l'une ou l'autre forme, reste bloquée.
4. Un plugin déprécié (`deprecated`) est installé et mis à jour comme les autres : la mention
   de l'index est lue à l'administrateur, jamais agie par l'hôte.

## Et l'état du plugin ?

L'état persistant d'un plugin est versionné (`payload_schema`). Une version qui change le
schéma embarque `migrate_state(from_version, key, payload)` ; le kit de conformance la réclame
et l'hôte l'appelle avant le premier appel de la nouvelle version. Le retour arrière
(`shelfhold plugin rollback`) est refusé, avec la raison `state_newer`, vers une version qui ne
sait pas lire l'état écrit par une version plus récente : l'état n'est jamais corrompu par un
code qui ne le comprend pas.
