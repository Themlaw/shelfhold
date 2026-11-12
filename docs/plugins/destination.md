# Écrire une destination (contrat provisoire jusqu'à M4)

Une **destination** reçoit des fichiers rangés par Shelfhold (une bibliothèque Kavita, un dossier surveillé).
Le contrat vit dans `shelfhold.sdk.export_target` et `shelfhold.sdk.fsops` (licence MIT).

> **Provisoire.** Seule la forme du plan d'opérations (`FsOp`) est figée par le SDK 1.3. Le reste du contrat
> (éléments d'export, plages de versions, événements, progression) est encore décrit sans type et changera
> avec la première destination complète, en M4 ; certains membres propres à Kavita sont dépréciés depuis le
> SDK 1.1. Ne publiez pas de destination tierce avant M4. Le kit de conformité des destinations est
> **à venir en M4** ([kit.md](kit.md)).

## Ce que fait la destination, ce que fait l'hôte

La destination ne touche jamais un fichier et ne détient jamais de clé. Elle rend des **valeurs** ou des
**descriptions d'appels** (`ApiCall`) et un **plan d'opérations** ; l'hôte les valide, puis les exécute avec ses
propres secrets, par son `Fetcher`.

## Le plan d'opérations

`FsOpPlan` est du JSON : une suite d'`FsOp` (au plus 10 000), tous les chemins **relatifs à la racine de la
liaison**, aucun contenu (un `ContentRef` désigne des octets que l'hôte possède). Opérations :
`mkdir`, `write`, `write_inline`, `rename`, `recycle`, `touch_mtime`, `unlink`.

Sont refusés quoi que dise le plugin :

- un chemin **non enraciné** : absolu, lettre de lecteur, `..`, segment `.` ou vide, caractère de contrôle ;
- un chemin **réservé** : la zone `.shelfhold` de l'hôte et ce qu'il garde à côté (`.shelfhold-mount-id`,
  `.shelfhold-probe-*`), sous tout déguisement (casse, forme Unicode, séparateur, point ou espace final), à toute
  profondeur, et tout nom court 8.3 ;
- **`unlink`**, réservé à l'hôte : une destination peut recycler, jamais supprimer pour de bon. L'opération reste dans `FsOp` parce que **l'hôte** construit la sienne (suppression d'une purge) ; ce n'est pas un membre d'un plan de destination ;
- l'écrasement : `rename` n'écrase jamais, `write` refuse un chemin qui existe et que le plugin n'a pas écrit ;
  un plan qui nomme deux fois une même cible, quelle que soit la casse, est refusé avant exécution.

```python
from shelfhold.sdk.fsops import FsOp, FsOpPlan, parse_destination_plan

plan = FsOpPlan(ops=(FsOp(op="mkdir", rel_path="Serie A"),))
host_copy = parse_destination_plan(plan.model_dump_json())  # la copie que l'hôte exécute
```

L'hôte exécute la copie que rend `parse_destination_plan`, jamais l'objet remis par le plugin, et rappelle
`check_destination_plan` juste avant chaque exécution. Les modèles sont figés ; une sous-classe d'`FsOp` est
refusée.

## Capacités déclarées

`TargetCapabilities` : types de médias et formats, transport, sorties (`egress`), `can_update`, `can_delete`,
`can_purge`, `readback` (`none`, `verify`, `fetch`), sidecars, `access_partitioning`, événements, collections,
progression, contraintes de nommage. `can_purge`, `readback`, `render()` et `write_inline` sont figés avec le
SDK 1.3.

## Événements et états d'attente

Un plugin n'émet que sous **`plugin.<clé>.*`**, sa propre clé : jamais `job.failed` ni l'espace d'un autre
plugin. Chaque état d'attente propre déclare sa sortie : `retry`, `recheck` ou `reconfigure`.
