# États d'attente et commandes de sortie

Quand Shelfhold ne peut pas continuer sans vous, il **attend** : il n'insiste pas, ne réessaie pas
en boucle, ne contourne rien. Chaque état d'attente a une commande qui en sort (EF-INT-023), la
même par la ligne de commande, par la socket locale et par l'API.

## Où lire ce qui attend

- `shelfhold status` (ou `--json`) : chaque attente avec sa raison, l'élément concerné, la commande
  à lancer, et les files en pause. Lecture seule, sans verrou ; fonctionne serveur arrêté.
- `GET /api/v1/status` : même contenu, administrateur seulement.
- `GET /healthz` : état général ; authentifié, il ajoute la liste des files en pause et leur
  raison. Anonyme, il ne montre que l'état (`ok`, `degraded`, `migrating`).
- Les événements d'exploitation vont aussi vers vos notifications ([notifications.md](notifications.md)).

Une commande **réservée** exige une session administrateur dont le mot de passe a été saisi il y a
moins de 15 minutes, ou la ligne de commande locale ; une clé d'API est refusée
(`credential-not-allowed`). Voir [durcir.md](durcir.md) pour la fraîcheur.

## Les états de la version 0.2

| État | Ce qui s'est passé | Commande de sortie | Réservée |
| --- | --- | --- | --- |
| `missing` | Un fichier a disparu de la destination | `shelfhold missing list`, puis `missing resolve <destination> <élément> --reexport` ou `--unfollow` | non |
| `paused_foreign_writer` | Un autre écrivain modifie les dossiers de la série | `shelfhold series resume <série>` | non |
| `dead_letter` | Une tâche a épuisé ses tentatives | `shelfhold jobs list --status dead_letter`, `jobs retry <tâche>` | non |
| `instance_changed` | Kavita a été réinstallé (empreinte différente) | `shelfhold target reconnect <cible>` | non |
| `target_forbidden` | Le compte Kavita n'a plus les droits | rendez les droits dans Kavita, puis `target reconnect <cible>` | non |
| `key_expiring` | La clé nommée de Kavita expire dans 14 jours ou moins | `shelfhold target rotate-key <cible> --password-file <fichier>` | oui |
| `not_indexed` | Le fichier écrit n'apparaît pas dans Kavita | corrigez l'exclusion, le type ou la surveillance de dossier de Kavita, puis `jobs retry <tâche>` | non |
| `auth_expired` | Suwayomi refuse les identifiants | `shelfhold source set-secret --secret-file <fichier>` | oui |
| `auth_mode_unsupported` | Suwayomi est en mode `ui_login` | passez-le en `none` ou `basic_auth`, puis `shelfhold source recheck` | non |
| `adoption_refused` | Bascule non confirmée | `shelfhold adopt --confirm-migration` **[câblage]**, après [migration-depuis-scripts.md](migration-depuis-scripts.md) | non |
| `coverage_unknown` | Série en volumes seuls : point de départ du suivi inconnu | `shelfhold adopt set-follow-from` **[câblage]** | non |
| `permission_denied` | Shelfhold ne peut pas écrire dans le dossier d'une série | appliquez la correction affichée (ACL, `group_add`, `user:`), puis `shelfhold target recheck <cible>` | non |
| `child_exposure` | Un compte Kavita d'enfant voit la bibliothèque | retirez l'accès dans Kavita, puis `target recheck <cible>` | oui |
| `route_missing` | Kavita n'a pas une fonction attendue | mettez Kavita à jour, puis `target recheck <cible>` | non |
| `language_unknown` | Une Édition adoptée n'a pas de langue | `shelfhold edition set-lang <édition> <langue>` (ex. `fr`, `ja`, `pt-BR`) | non |
| `restored_behind` | La base est en retard sur les fichiers : lecture seule | `shelfhold reconcile --adopt-after-restore`, voir [restauration.md](restauration.md) | seule la suppression confirmée |
| `tombstone_key_unknown` | Un journal de pierres tombales est chiffré par une clé absente du trousseau | importer le trousseau d'origine, ou `shelfhold tombstone accept-lost-keyring --confirm "the keyring is lost"` | oui |

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

## Si rien n'aide

Le journal structuré contient le code d'erreur de l'événement (par ex. `target.forbidden`), dont
le texte français et l'action recommandée sont dans le catalogue d'erreurs de l'API. Vérifiez que
`/readyz` répond et que `shelfhold status` n'a pas d'avertissement de trousseau.
