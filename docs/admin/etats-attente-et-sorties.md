# États d'attente et commandes de sortie

Quand Shelfhold ne peut pas continuer sans vous, il **attend** : il n'insiste pas, ne réessaie pas
en boucle, ne contourne rien. Chaque état d'attente a une commande qui en sort (EF-INT-023), la
même par la ligne de commande, par la socket locale et par l'API.

## Où lire ce qui attend

- `shelfhold status` (ou `--json`) : chaque attente avec sa raison, l'élément concerné, la commande
  à lancer, et les files en pause. Lecture seule, sans verrou ; fonctionne serveur arrêté.
  `adoption_refused` et `coverage_unknown` y figurent (ils sont inscrits en base par l'adoption et
  l'appariement, donc visibles depuis n'importe quel processus), avec leur commande de sortie.
  Sous les attentes, `status` affiche aussi, sans que ce soient des attentes :
  - les **séries silencieuses** (`series.stale`) : aucun chapitre découvert depuis trois fois la
    cadence observée de la série, ou depuis 14 jours quand elle n'a pas de cadence mesurable ; une
    série terminée, annulée ou mise en pause par vous n'est jamais comptée ;
  - les **séries purgées encore présentes dans Suwayomi** (`series.tombstoned_upstream`) : Shelfhold
    les ignore, mais en M1a il ne les retire pas de Suwayomi ; faites-le à la main, sinon elles
    resteront listées ;
  - par racine physique, ce que le disque permet (`reflink`, lien dur vers `/store`, inodes
    stables), sondé au démarrage du serveur et sans rien laisser sur la racine
    ([topologie-racines-physiques.md](topologie-racines-physiques.md)) ;
  - l'âge de la liste `known_bad_versions` (versions de Kavita connues pour une faille) livrée avec
    la version de Shelfhold, la version minimale de Kavita prise en charge, et les cibles dont la
    version figure dans la liste ;
  - les avertissements du trousseau, dont une clé maîtresse qui vit dans `/data`
    ([secrets.md](secrets.md)).
- `GET /api/v1/status` : même contenu, administrateur seulement.
- `GET /healthz` : état général ; authentifié, il ajoute la liste des files en pause et leur
  raison. Anonyme, il ne montre que l'état (`ok` ou `migrating`).
- Les événements d'exploitation vont aussi vers vos notifications ([notifications.md](notifications.md)).

Une commande **réservée** exige une session administrateur dont le mot de passe a été saisi il y a
moins de 15 minutes, ou la ligne de commande locale ; une clé d'API est refusée
(`credential-not-allowed`). Voir [durcir.md](durcir.md) pour la fraîcheur.

## Les états

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
| `source_instance_changed` | Suwayomi a une autre empreinte : il a été réinstallé. Le suivi est en pause ; rien n'est supprimé ni redemandé | remettez vos séries dans le nouveau Suwayomi si besoin, puis `shelfhold source reconnect` (API : `POST /api/v1/sources/suwayomi/reconnect`) : la nouvelle installation devient la référence et les séries sont retrouvées par leur identité amont | non |
| `auth_mode_unsupported` | Suwayomi est dans un mode que cette version ne pilote pas, ou le compte de service peut installer des extensions | passez-le en `none`, `basic_auth` ou `ui_login` avec un compte aux droits minimaux, puis `shelfhold source recheck` | non |
| `adoption_refused` | Bascule non confirmée | `shelfhold adopt --confirm-migration`, après [migration-depuis-scripts.md](migration-depuis-scripts.md) | non |
| `coverage_unknown` | Série en volumes seuls : point de départ du suivi inconnu | `shelfhold adopt set-follow-from` | non |
| `permission_denied` | Shelfhold ne peut pas écrire dans le dossier d'une série | appliquez la correction affichée (ACL, `group_add`, `user:`), puis `shelfhold target recheck <cible>` | non |
| `child_exposure` | Un compte Kavita d'enfant voit la bibliothèque | retirez l'accès dans Kavita, puis `target recheck <cible>` | oui |
| `route_missing` | Kavita n'a pas une fonction attendue | mettez Kavita à jour, puis `target recheck <cible>` | non |
| `language_unknown` | Une Édition adoptée n'a pas de langue | `shelfhold edition set-lang <édition> <langue>` (ex. `fr`, `ja`, `pt-BR`) | non |
| `series_name_collision` | Le nom de série d'une Édition est déjà pris dans la bibliothèque Kavita (par exemple deux Œuvres du même titre, `en` et `fr`) : rien n'est exporté pour elle, ni écrasé | `shelfhold edition set-series-name <édition> "<nom>"` (API : `POST /api/v1/editions/{id}/series-name`) | non |
| `mount_sentinel_missing` | La sentinelle `.shelfhold-mount-id` d'une racine manque ou n'est pas la bonne (volume non monté, disque vide) : écritures et scans bloqués | montez le bon volume, puis `shelfhold target recheck <cible>` | non |
| `rating_pending` | Une Œuvre a une Édition non classée : elle est retenue, ou attend sur une étagère famille (affiché seulement quand une étagère famille existe) | `shelfhold rate <âge> --work <id>` (toutes d'un coup : `rate <âge> --pending`), voir [a-classer.md](a-classer.md) | oui (admin ; mot de passe récent seulement en profil durci) |
| `follow_suspended` | Une Œuvre adoptée n'est pas suivie tant qu'elle n'est pas classée | idem : `shelfhold rate <âge> --work <id>` | oui (idem) |
| `withdrawal_pending` | Un fichier attend de quitter une étagère famille (classement relevé, ou un export tenait le verrou) | `shelfhold shelf withdraw`, puis `--apply --confirm <version>` ; jamais automatique | oui |
| `safeguard_backlog` | La mise à l'abri n'a pas tout copié dans son quota (200 Unités, 5 Gio, place de `/store`) | rien à faire : elle se résorbe de nuit en nuit ; `shelfhold jobs list` ; libérez de la place dans `/store` si elle dure | non |
| `tombstone_lines_owed` | Une racine démontée doit encore des lignes à son journal de pierres tombales ; aucune écriture vers elle | remontez la racine, puis `shelfhold tombstone flush` | non |
| `restored_behind` | La base est en retard sur les fichiers : lecture seule | `shelfhold reconcile --adopt-after-restore`, voir [restauration.md](restauration.md) | seule la suppression confirmée |
| `restored_credentials` | Une restauration a révoqué toutes les sessions et clés d'API ; la connexion par passkey est suspendue ; mots de passe possiblement revenus à l'état de la sauvegarde | revoyez les passkeys, puis `shelfhold restore acknowledge` (CLI de l'hôte uniquement ; liste les passkeys changées) (la route API refuse toute session ou clé), voir [sauvegarde.md](sauvegarde.md) | oui |
| `tombstone_key_unknown` | Un journal de pierres tombales est chiffré par une clé absente du trousseau | `shelfhold secrets import --passphrase-file <fichier> --in <export>` (voir [secrets.md](secrets.md)), ou remettre le fichier de trousseau d'origine à sa place ; en dernier recours `shelfhold tombstone accept-lost-keyring --confirm "the keyring is lost"` | oui |
| `tombstone_journal_truncated` | Le journal de pierres tombales d'une racine s'arrête avant sa dernière ligne (lignes de fin supprimées) : rapprochement et réacquisition bloqués sur cette racine seulement, rien n'est supprimé | `shelfhold tombstone set-aside-journal --root <racine>` : le fichier est mis de côté (`tombstones.aside-<date>`) et le journal est réécrit depuis la base | oui |
| `foreign_root` | La sentinelle d'une racine dit une autre instance, ou ne peut pas être crue | `shelfhold root takeover <racine>` (ou `root reinit`) | oui |
| `root_lost` | Une racine n'a plus de sentinelle : disque remplacé, fichier retiré | `shelfhold root reinit <racine>` (ou `root takeover` si le contenu est intact) | oui |
| `root_behind` | Une racine est plus ancienne que la base : une vieille copie a été remise | `shelfhold root restored <racine>` | non |
| `store_lost` | La sentinelle de `/store` manque, est étrangère ou en retard | `shelfhold store reinit` | oui |

Notes :

- `series resume` sur une série `permission_denied` relance la sonde d'écriture et refuse avec la
  correction si elle échoue.
- `tombstone accept-lost-keyring` lève le blocage de réacquisition pour les clés manquantes **d'aujourd'hui**
  seulement ; une clé perdue plus tard n'est pas pardonnée d'avance. La phrase de confirmation se tape à l'identique, en anglais ; pendant `restored_behind` elle est plus longue et énonce le coût (voir [secrets.md](secrets.md)).
- Les quatre états des racines sont détectés racine par racine, au démarrage et à chaque écriture
  refusée, et listés par `shelfhold status`.
  Aucune de ces commandes ne supprime un fichier, et aucune ne touche à `.shelfhold/` autrement
  qu'en y ajoutant (voir [sinistre-sans-sauvegarde.md](sinistre-sans-sauvegarde.md) et
  [topologie-racines-physiques.md](topologie-racines-physiques.md)).

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
