# Écrire une destination (SDK 1.4, contrat partiellement provisoire)

Une **destination** reçoit des fichiers rangés par Shelfhold (une bibliothèque Kavita, un dossier surveillé).
Le contrat vit dans `shelfhold.sdk.export_target`, `shelfhold.sdk.api_call` et `shelfhold.sdk.fsops` (licence MIT).

> **Partiellement provisoire.** Le plan d'opérations (`FsOp`, SDK 1.3) et le corps d'`ApiCall` (SDK 1.4) sont figés. Le reste du contrat
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
`can_purge`, `readback`, `content_body`, `update_metadata`, sidecars, `access_partitioning`, événements,
collections, progression, contraintes de nommage. Les ajouts du SDK 1.4 sont **tous facultatifs** : une destination
écrite pour le SDK 1.3 se charge et fonctionne sans la moindre modification.

- `can_purge` (faux par défaut) : suppression définitive, distincte de la corbeille propre à la destination
  (`can_delete`). Aucune des deux n'autorise `unlink` dans un plan rendu par la destination.
- `readback` (`none` par défaut) : `none`, un exemplaire en destination n'est jamais relu ; `verify`, il sert à
  vérifier une copie ; `fetch`, il peut aussi servir de source d'octets.
- `content_body` (faux par défaut) : la destination envoie des octets par `ApiCall.body`. Condition nécessaire du
  transport `push`.
- `update_metadata` (absent par défaut) : `MetadataUpdateSupport(fields, lock)`, les champs qu'elle change sur
  place et si elle sait les verrouiller contre un nouveau scan ; la méthode `update_metadata(ref, fields, lock)`
  (protocole `MetadataUpdater`, à part d'`ExportTarget`) rend les appels. **Absente, elle n'est jamais appelée** :
  un changement de métadonnées devient une **proposition de réécriture**, avec l'avertissement (`metadata_change_route`).

## Le corps d'un `ApiCall`

Un `ApiCall` porte un chemin **relatif** (l'hôte est fixé par la configuration de la destination), une méthode,
une `query` et un `auth` (`target_admin`, `target_user`, `none`) que le cœur remplace par le secret : un appel n'a
aucun champ d'en-tête. Son corps est `json_body` (SDK 1.3, toujours accepté) ou `body` :

- `JsonBody(value)` : du JSON ;
- `ContentBody(content_ref, encoding)` : des octets que l'hôte possède, bruts (`RawEncoding(content_type)`) ou en
  multipart (`MultipartEncoding(file_field, fields)`). Les deux corps ensemble sont refusés.

Un corps de contenu suit **les mêmes règles que `FsOp.write`** (`check_content_ref`, `check_api_call`) : la
référence est un `export_item` planifié pour cet appel et cette liaison ; un fichier tatoué ne part jamais vers une
destination `egress = externe` ; le miroir de provenance n'est jamais envoyé. Un chemin avec hôte, `//`, `..`,
requête ou fragment est refusé. Aucune valeur d'un appel (query, corps, champ multipart) n'apparaît dans un
`repr`, une erreur de validation ou un refus de l'hôte.

## Le transport `push`

`push` reste refusé par le kit (`run_destination_declaration_check`) tant que la destination ne déclare pas
`content_body`, et tant qu'elle ne passe pas le kit des destinations (M4, L05). Il ne donne jamais d'accès réseau
à l'adaptateur : tout part par le `Fetcher` du cœur et sa garde SSRF.

## Événements et états d'attente

Un plugin n'émet que sous **`plugin.<clé>.*`**, sa propre clé : jamais `job.failed` ni l'espace d'un autre
plugin. Chaque état d'attente propre déclare sa sortie : `retry`, `recheck` ou `reconfigure`.
