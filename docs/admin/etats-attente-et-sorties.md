# États d'attente et commandes de sortie

Quand Shelfhold ne peut pas continuer sans vous, il **attend** : il n'insiste pas, ne réessaie pas
en boucle, ne contourne rien. Chaque état d'attente a une commande qui en sort (EF-INT-023), la
même par la ligne de commande, par la socket locale et par l'API.

## Où lire ce qui attend

- `shelfhold status` (ou `--json`) : chaque attente avec sa raison, l'élément concerné, la commande
  à lancer, et les files en pause. Lecture seule, sans verrou ; fonctionne serveur arrêté.
  `adoption_refused` et `coverage_unknown` n'y figurent pas encore : ils sont portés par
  l'événement et la notification, et leur commande de sortie est dans le tableau ci-dessous.
- `GET /api/v1/status` : même contenu, administrateur seulement.
- `GET /healthz` : état général ; authentifié, il ajoute la liste des files en pause et leur
  raison. Anonyme, il ne montre que l'état (`ok` ou `migrating`).
- Les événements d'exploitation vont aussi vers vos notifications ([notifications.md](notifications.md)).

Une commande **réservée** exige une session administrateur dont le mot de passe a été saisi il y a
moins de 15 minutes, ou la ligne de commande locale ; une clé d'API est refusée
(`credential-not-allowed`). Voir [durcir.md](durcir.md) pour la fraîcheur.

## Les états de la version 0.2

| État | Ce qui s'est passé | Commande de sortie | Réservée |
| --- | --- | --- | --- |
| `missing` | Un fichier a disparu de la destination | `shelfhold missing list`, puis `missing resolve <destination> <élément> --reexport` ou `--unfollow` | non |
| `paused_foreign_writer` | Un autre écrivain modifie les dossiers de la série | `shelfhold series resume <série exportée>` (l'identifiant que `status` donne) | non |
| `dead_letter` | Une tâche a épuisé ses tentatives | `shelfhold jobs list --status dead_letter`, `jobs retry <tâche>` | non |
| `instance_changed` | Kavita a été réinstallé (empreinte différente) | `shelfhold target reconnect <cible>` | non |
| `target_forbidden` | Le compte Kavita n'a plus les droits | rendez les droits dans Kavita, puis `target reconnect <cible>` | non |
| `key_expiring` | La clé nommée de Kavita expire dans 14 jours ou moins | `shelfhold target rotate-key <cible> --password-file <fichier>` | oui |
| `not_indexed` | Le fichier écrit n'apparaît pas dans Kavita | corrigez l'exclusion, le type ou la surveillance de dossier de Kavita, puis `jobs retry <tâche>` | non |
| `auth_expired` | Suwayomi refuse les identifiants | `shelfhold source set-secret --secret-file <fichier>` | oui |
| `auth_mode_unsupported` | Suwayomi est en mode `ui_login` | passez-le en `none` ou `basic_auth`, puis `shelfhold source recheck` | non |
| `adoption_refused` | Bascule non confirmée | `shelfhold adopt --confirm-migration`, après [migration-depuis-scripts.md](migration-depuis-scripts.md) | non |
| `coverage_unknown` | Série en volumes seuls : point de départ du suivi inconnu | `shelfhold adopt set-follow-from` | non |
| `permission_denied` | Shelfhold ne peut pas écrire dans le dossier d'une série | appliquez la correction affichée (ACL, `group_add`, `user:`), puis `shelfhold target recheck <cible>` | non |
| `child_exposure` | Un compte Kavita d'enfant voit la bibliothèque | retirez l'accès dans Kavita, puis `target recheck <cible>` | oui |
| `route_missing` | Kavita n'a pas une fonction attendue | mettez Kavita à jour, puis `target recheck <cible>` | non |
| `language_unknown` | Une Édition adoptée n'a pas de langue | `shelfhold edition set-lang <édition> <langue>` (ex. `fr`, `ja`, `pt-BR`) | non |
| `restored_behind` | La base est en retard sur les fichiers : lecture seule | `shelfhold reconcile --adopt-after-restore`, voir [restauration.md](restauration.md) | seule la suppression confirmée |
| `tombstone_key_unknown` | Un journal de pierres tombales est chiffré par une clé absente du trousseau | remettre le fichier de trousseau d'origine à sa place (`/data/keyring`), ou `shelfhold tombstone accept-lost-keyring --confirm "the keyring is lost"` | oui |

Notes :

- `series resume` sur une série `permission_denied` relance la sonde d'écriture et refuse avec la
  correction si elle échoue.
- `tombstone accept-lost-keyring` lève le blocage de réacquisition pour les clés manquantes **d'aujourd'hui**
  seulement ; une clé perdue plus tard n'est pas pardonnée d'avance. La phrase de confirmation se tape à l'identique, en anglais. L'import d'un trousseau exporté (`secrets import`) n'est pas livré
  dans cette version.
- Les états qui concernent les racines physiques (`foreign_root`, `root_lost`, `root_behind`) et
  `/store` (`store_lost`), la notation et la mise à l'abri arrivent avec M1b. Leurs commandes
  (`root takeover|reinit`, `store reinit`) sont décrites dans
  [topologie-racines-physiques.md](topologie-racines-physiques.md).

## Intentions de fichiers restées ouvertes

Une opération de fichier en plusieurs étapes est inscrite dans le journal d'intentions avant de
toucher au disque. Au démarrage, avant toute tâche, chaque intention ouverte est réglée : achevée
ou rejouée seulement si le disque correspond exactement à une étape connue. Une intention dont le
disque ne correspond à rien reste ouverte (`reconcile.drift`), sans que rien soit touché.

- `shelfhold intents list` : les intentions ouvertes (opération, état, chemins).
- `shelfhold intents resolve <intention>` : applique la règle du démarrage à cette seule intention.
  Si le disque ne correspond toujours pas, la commande sort avec le code 3 et l'intention reste
  ouverte.
- `shelfhold intents resolve <intention> --abandon --confirm <intention>` : renonce à l'intention
  **sans toucher au disque**. Ses chemins cessent d'être protégés contre le planificateur et le
  rapprochement : à ne faire qu'après avoir regardé les fichiers à la main.

`intents resolve` est une action réservée : session récente ou CLI de l'hôte, jamais une clé d'API,
et chaque appel est inscrit à l'audit (qui, quelle session, quand). La route est
`POST /api/v1/intents/{id}/resolve`, la liste `GET /api/v1/intents`.

## Migrer un normaliseur

Une clé d'identité (la place d'un chapitre, par exemple) est produite par un normaliseur
versionné, et chaque ligne garde la version qui l'a produite. Quand une version plus récente est
livrée, les anciennes lignes restent valides tant que vous ne les avez pas migrées :

- `shelfhold normalize migrate number` : aperçu, sans rien écrire. Il liste les lignes qui
  changeraient de version, les **fusions** à décider (deux chapitres viseraient la même place ;
  ils restent tels quels) et les lignes **bloquées par une pierre tombale** (elles ramèneraient un
  contenu purgé ; `tombstone.migration_hit` est émis, la ligne reste telle quelle). Il donne une
  empreinte.
- `shelfhold normalize migrate number --confirm <empreinte>` : applique exactement cet aperçu, en
  une transaction avec son entrée d'audit. Une empreinte périmée ne change rien (code 4).

C'est une action réservée : session récente ou CLI de l'hôte, jamais une clé d'API. Seul `number`
est migrable en M1a. Tant que le code ne livre aucun normaliseur plus récent que `number@1`, la commande répond
« rien à migrer » ; aucun code extérieur n'est chargé au démarrage. Une version citée par une pierre tombale n'est jamais retirée du code.

## Rattraper l'historique d'une série suivie

`shelfhold backfill --gaps <édition>` comble les numéros manquants sous le filigrane de suivi ;
`--all` ramène tout l'historique. La commande affiche toujours l'estimation (nombre de chapitres,
taille, espace libre). Au-delà de 100 chapitres ou de 5 Gio, elle ne lance rien tant qu'on ne lui
redonne pas les chiffres vus : `--confirm-units N --confirm-bytes OCTETS` (code de sortie 3 sinon,
avec la commande exacte). `--dry-run` n'affiche que l'estimation. Lancer ne télécharge rien d'un
coup : le suivi demande ces chapitres dans ses plafonds par cycle. Route :
`GET|POST /api/v1/editions/{édition}/backfill`.

## Opérations longues de l'API

Appliquer un plan (`POST /api/v1/plans/{id}/apply`, avec le `If-Match` de l'aperçu) répond
`202 Accepted` et `Location: /api/v1/jobs/{tâche}` ; `GET /api/v1/jobs/{tâche}` donne l'état
(`queued`, `running`, `succeeded`, `failed`) et, en cas d'échec, le code du catalogue (`plan.stale`,
`target.route_missing`...). Les refus qui n'ont pas besoin du disque (412, 428, destination en
pause) sont donnés avant qu'une tâche existe. Une clé d'idempotence est gardée 24 h pour le même
identifiant (une clé d'API garde la sienne tout ce temps ; une session, qui est un identifiant à
part et dure moins, ne la retrouve pas après une nouvelle connexion).

## Si rien n'aide

Le journal structuré contient le code d'erreur de l'événement (par ex. `target.forbidden`), dont
le texte français et l'action recommandée sont dans le catalogue d'erreurs de l'API. Vérifiez que
`/readyz` répond, que `shelfhold status` n'a pas d'avertissement et que `shelfhold secrets status`
n'en a pas non plus (trousseau).
