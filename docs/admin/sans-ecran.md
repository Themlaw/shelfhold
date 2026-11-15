# Supprimer, restaurer, fusionner et relier sans écran

En M3, l'aperçu, l'exécution et la restauration de la suppression à trois niveaux, les propositions
« Même œuvre ? » et les plans de fusion ou de réaffectation n'ont pas d'écran : la détection et les
API sont livrées, l'écran est reporté en M4. Toutes ces actions restent possibles pour un
administrateur, soit par les commandes `shelfhold` réservées à l'hôte, soit par les routes HTTP de
l'API avec une session administrateur fraîche. `shelfhold <commande> --help` fait foi sur ce que
votre version propose.

Rien ici ne contourne un garde-fou : les actions réservées (suppression, fusion, réaffectation)
demandent toujours un mot de passe local saisi dans la même session de navigateur, ou passent par
la CLI de l'hôte. Une clé d'API ne suffit jamais.

## Supprimer

### Suppression à trois niveaux (corbeille, 30 jours)

Les trois niveaux sont `unfollow`, `remove_destinations` et `delete_content`. La suppression se fait
en deux temps : un aperçu, puis l'application avec le jeton (`version`) rendu par l'aperçu.

1. Aperçu :

   `POST /api/v1/deletions/preview`

   corps : `{"level": "unfollow|remove_destinations|delete_content", "unit_ids": ["…", …]}` (1 à 500
   identifiants).

2. Application :

   `POST /api/v1/deletions`

   corps : `{"level": "…", "unit_ids": ["…"], "version": "<jeton de l'aperçu>", "typed": {"<unit_id>": "<identifiant tapé>"}}`.

   L'identifiant tapé est l'identifiant de l'Unité, pour chaque Unité ; c'est la confirmation tapée
   pour les Unités irrémissibles.

Restauration depuis la corbeille :

`POST /api/v1/units/{unit_id}/restore`

corps facultatif : `{"deletion_id": "<une suppression>", "partial": true|false}`. Sans
`deletion_id`, la dernière suppression est restaurée ; avec `partial: true`, les fichiers qui peuvent
revenir reviennent, les autres restent dans la corbeille.

Vider la corbeille (commande hôte) :

`shelfhold trash empty --confirm empty-trash`

ou par l'API : `POST /api/v1/trash/empty` avec `{"typed": "empty-trash"}`. La liste de la corbeille
est lisible par `GET /api/v1/trash`.

### Effacement définitif (purge)

`shelfhold purge --now --unit <id> --confirm <id>`

`shelfhold purge --now --work <id> --confirm <id>`

La purge écrit d'abord la pierre tombale, la rend durable, puis efface les fichiers ; ce qui ne peut
pas être atteint est dit à la fin. L'identifiant est tapé de nouveau comme confirmation, qu'il
s'agisse ou non d'une Unité irrémissible. Les autres formes de la commande sont `--leftovers`,
`--checkpoint` et `--kept-ack` (voir `purge.md`).

Par l'API : `POST /api/v1/units/{unit_id}/purge` et `POST /api/v1/works/{work_id}/purge` avec
`{"typed": "<id>"}`.

## Restaurer

Après une restauration de sauvegarde (`shelfhold restore` d'archive), les sessions et les clés d'API
sont révoquées : il faut les réexaminer, puis dire que c'est fait :

`shelfhold restore acknowledge`

La restauration d'une Unité supprimée à trois niveaux se fait par
`POST /api/v1/units/{unit_id}/restore` (voir ci-dessus). Pour une racine restaurée sans que la base
le sache, voir `root restored`.

## Fusionner

### Éditions (commande hôte)

`shelfhold edition merge <keep> <drop> [--plan | --apply <empreinte>]`

Sans `--plan` ni `--apply`, la commande affiche le plan ; `--plan` force l'aperçu ; `--apply` avec
l'empreinte rendue par le plan applique exactement ce plan (une empreinte périmée ne change rien).

`shelfhold edition merge-proposals <keep> <drop>`

Propose les fusions de contributeurs entre deux Éditions (à confirmer par ailleurs).

### Œuvres (API)

`GET /api/v1/merge-suggestions/works` liste les suggestions ; `POST /api/v1/merge-suggestions/works/scan`
les recalcule.

`POST /api/v1/works/merge` avec `{"keep_id": "…", "drop_id": "…", "confirm": "<empreinte ou null>"}` :
sans empreinte, renvoie le plan ; avec l'empreinte du plan, l'applique.

`POST /api/v1/works/merges/{merge_id}/separate` défait une fusion déjà appliquée.

`POST /api/v1/series-reassignments` avec `{"work_id": "…", "confirm": "<empreinte ou null>"}`
réaffecte la série d'une Œuvre.

`POST /api/v1/works/{work_id}/series` avec `{"series_id": "…", "position": <n>}` rattache une série
à une Œuvre qui n'en avait pas.

Fusion d'Éditions par l'API : `POST /api/v1/editions/merge` avec
`{"keep_id": "…", "drop_id": "…", "confirm": "<empreinte ou null>"}`.

## Relier

Relier une nouvelle liaison de source à une Œuvre existante (après une migration de source dans
Suwayomi) :

`shelfhold relink <work_id> --source-key <clé> --remote-id <identifiant distant> [--replaces <ancienne liaison>] [--lang <langue>]`

La commande rattache la nouvelle liaison, ne réexporte que les chapitres manquants et transporte les
alias d'équipe de l'ancienne liaison vers la nouvelle.

Les rattachements d'Édition par identifiant externe se gèrent par
`shelfhold edition attachments`, `shelfhold edition detach <attach_id>`,
`shelfhold edition attach-confirm <attach_id> --confirm <jeton>` et
`shelfhold edition attach-decline <attach_id>`.

## Ce qui reste à l'écran (M4)

Les propositions « Même œuvre ? », l'aperçu graphique de la suppression à trois niveaux et la
restauration depuis la corbeille n'ont pas d'écran en 1.0 ; cette page fait office, jusqu'à ce
qu'ils arrivent.