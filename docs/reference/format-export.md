# Format d'export intégral `shelfhold-export/1`

Ce document décrit le format ouvert produit par `shelfhold export-all` (EF-INT-016, ADR-0023/D18).

## Version

- `schema_version` / `format` : `shelfhold-export/1`

Le format est versionné. Un import refuse un schéma incompatible et ne s'exécute que sur une
instance neuve (base vide).

## Contenu

L'export est un répertoire contenant :

- `manifest.json` — métadonnées de l'export :
  - `schema_version` : `shelfhold-export/1`
  - `format` : `shelfhold-export/1`
  - `created_at` : horodatage UTC ISO 8601
  - `tables` : liste des tables exportées
  - `files` : entrées des fichiers copiés (identifiant, rétention, empreinte, chemin relatif)
- `data/<table>.json` — une liste d'objets JSON par table exportée (œuvres, éditions, séries,
  unités, variantes, étagères, états par profil, demandes, fichiers, etc.). Les secrets de
  destination ne sont pas exportés et les utilisateurs n'emportent aucun hash de mot de passe.
- `files/` — arbre des fichiers de contenu copiés (`files/store/...`, `files/library/...`) quand
  ils sont disponibles.

## Exclusions et sécurité

- **Aucun contenu sous pierre tombale** n'est exporté.
- **Aucune pierre tombale** (ni table, ni fichier, ni donnée qui nommerait un titre) n'est incluse
  dans l'export (EF-BIB-053 (5), EF-INT-027).
- Les tables de pierres tombales, de secrets, de sessions, de clés d'API et les données éphémères
  (file de jobs, intentions, verrous, journaux d'audit, caches) ne sont pas exportées.

## Import

`shelfhold export-all` produit l'archive ; l'import (`import_all`) la restaure dans une **instance
neuve uniquement**. Sur une base non vide, l'import est refusé (`NonEmptyInstanceError`) afin de ne
jamais mélanger des données. L'aller-retour sur une instance vierge restaure les entités et l'état
exportés.
