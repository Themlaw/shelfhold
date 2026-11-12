# Format d'export intégral `shelfhold-export/1`

Ce document décrit le format ouvert produit par `shelfhold export-all` et relu par
`shelfhold import-all` (EF-INT-016, EF-INT-027, ADR-0023, décision D18). Il suffit, avec un
interpréteur JSON, pour lire une bibliothèque sans Shelfhold.

## Les commandes

```
shelfhold export-all /chemin/absolu/vers/un/dossier
shelfhold import-all /chemin/absolu/vers/le/dossier
```

- Les deux sont des **actions réservées** (`export.all`, `import.all`) : la CLI de l'hôte, ou une
  session d'administrateur fraîche. Jamais une clé d'API (portée `admin` comprise), jamais un jeton
  d'appareil, jamais un profil qui n'est pas administrateur : l'export emporte l'historique de
  lecture et les demandes de **tous** les profils.
- Chaque exécution est **journalisée** (qui, quelle session, combien de lignes et de fichiers ;
  ni titre, ni nom de dossier).
- Le dossier de destination est **neuf ou vide**. Rien n'y est jamais écrasé ni supprimé ; il ne
  peut être ni sous `/data`, ni sous `/store`, ni sous une racine de la bibliothèque. Les fichiers
  sont créés en lecture et écriture pour leur propriétaire seul (0600, dossiers 0700).
- Si une racine n'est pas montée ou si un journal de pierres tombales ne se lit pas, l'export est
  **refusé** : ce qu'une pierre tombale couvre ne pourrait pas être laissé de côté.

## Version

`manifest.json` porte `format` et `schema_version`, tous deux égaux à `shelfhold-export/1`. Le
format est versionné : on n'y ajoute que des colonnes facultatives ; tout autre changement fait
passer à `shelfhold-export/2`. Un import refuse une version qu'il ne connaît pas, une table ou une
colonne inconnue.

## Contenu du dossier

- `manifest.json` — écrit **en dernier** : un dossier sans manifeste est un export interrompu, et
  l'import le refuse.
  - `created_at` : horodatage UTC (ISO 8601).
  - `tables` : pour chaque table exportée, `rows` (nombre de lignes) et `sha256` (empreinte du
    fichier `data/<table>.json`).
  - `files` : les fichiers copiés, avec `unit_id`, `location` (`library` ou `store`), `root`
    (nom de la racine), `path` (chemin relatif à la racine), `file` (chemin dans l'arbre),
    `size`, `sha256` et `matches_registry` (faux si les octets ne sont plus ceux que le registre
    des copies annonce : une copie abîmée est dite, jamais cachée).
  - `files_missing` : les fichiers que l'inventaire connaît et que le disque a perdus.
  - `excluded_tables` : les tables laissées de côté, par nom.
- `data/<table>.json` — une liste d'objets JSON par table, clés triées. Les dates sont en ISO 8601
  avec fuseau, les identifiants sont des ULID, les valeurs JSON de la base restent du JSON.
- `files/library/<racine>/…` et `files/store/…` — l'arbre des fichiers gérés, à leur chemin
  relatif dans leur racine.

## Tables exportées

Œuvres, Éditions, Unités, étagères, états par profil, demandes et fichiers (EF-INT-016) :

| Ensemble | Tables |
| --- | --- |
| Catalogue | `series`, `series_member`, `work`, `work_title`, `contributor`, `contributor_name`, `contribution`, `external_id`, `metadata_field` |
| Éditions et Unités | `edition`, `volume`, `team`, `team_priority`, `unit`, `unit_variant`, `variant_revision` |
| Sources liées | `source_item`, `source_binding`, `series_follow` |
| Fichiers | `content`, `export_item`, `unit_coverage` |
| Étagères | `shelf` |
| Profils | `user`, `user_state`, `reading_position`, `request`, `profile_source_policy` |

La table `user` perd sa colonne `password_hash` : **aucun mot de passe** ne sort. Les profils
importés n'en ont pas, et un administrateur en pose un avec `shelfhold admin reset-password`.

## Ce que l'export ne contient jamais

- **Aucune pierre tombale**, ni rien qui en nommerait le titre : pas de table `content_tombstone`
  ni de ses registres, et aucun chapitre, aucune variante, aucun fichier, aucune œuvre, aucune
  série d'origine qu'une pierre tombale couvre (portées `variant`, `place`, `series_binding` et
  `work`). Ce n'est pas seulement une purge terminée : une purge interrompue avant que ses lignes
  ne partent, ou une base revenue d'une sauvegarde antérieure, laisse en base des lignes qu'une
  pierre tombale couvre, et l'export les relit contre les pierres tombales elles-mêmes. Une œuvre
  dont tous les chapitres sont couverts n'est pas exportée ; une série qui n'a plus d'œuvre, ni un
  contributeur qui n'était crédité que sur elle, non plus. Une référence vers ce qui est resté
  dehors (une Édition « traduite de ») est coupée, jamais laissée pendante.
- **Aucun secret** : ni clé d'API, ni jeton, ni session, ni passkey, ni mot de passe, ni
  phrase de passe, ni trousseau, ni valeur chiffrée (`api_key`, `session`, `webauthn_credential`,
  `secret_key`, `export_target`, `source_account`, `source_secret`, `source_config`,
  `plugin_attestation`, `idempotency_key`).
- **Aucune destination** : les cibles, leurs identifiants et les chemins de l'hôte restent chez lui.
- **Ni file d'attente, ni journal, ni cache, ni index** : ce que l'instance reconstruit seule (le
  inventaire des copies, l'index de recherche, les séries exportées d'une destination).
- Les originaux des EPUB non fiables (`untrusted/`) et les copies de destination.

Le test d'architecture `test_every_table_is_either_exported_or_left_out_with_a_reason` refuse une
table qui ne serait ni dans l'une ni dans l'autre liste : une table ajoutée plus tard est décidée,
jamais emportée par défaut.

## Import

`import-all` ne s'exécute que dans une **instance neuve** : migrée, avec ses racines enregistrées
(`shelfhold target add`), et aucune des tables ci-dessus ne porte une ligne. Sinon l'import est
**refusé** et rien n'est mélangé. Il ne se fait pas non plus en l'absence du manifeste.

L'archive n'est pas crue sur parole : le manifeste, la version, l'empreinte de chaque table et de
chaque fichier, les noms de colonnes et les chemins sont vérifiés avant la moindre écriture ; une
colonne `password_hash`, une table inconnue, un chemin qui sort de sa racine, un lien symbolique
ou un fichier modifié sont des refus. Les lignes entrent dans **une seule transaction** (tout ou rien), essayée puis annulée avant
que le premier fichier soit posé : une ligne que le schéma refuse n'écrit aucun fichier. Les
fichiers sont posés par la couche de fichiers de l'instance, jamais par-dessus un fichier
présent, et vérifiés contre leur empreinte. Le journal d'audit enregistre l'import dans la
transaction des lignes.

Après l'import : `shelfhold admin reset-password` pour chaque profil, `shelfhold search rebuild`
pour l'index de recherche, puis `shelfhold reconcile` pour que l'instance recense les fichiers
restaurés. Les destinations (Kavita) se recréent avec `shelfhold target add` et `shelfhold shelf` :
elles ne font pas partie de l'export.
