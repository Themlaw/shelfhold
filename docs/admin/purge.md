# Effacer pour de bon : `purge --now` et pierres tombales

Supprimer un titre de la bibliothèque (corbeille, suppression ordinaire) laisse la possibilité de
le retrouver. **Purger** est l'inverse : toutes les copies que Shelfhold gère disparaissent, et
**rien ne revient** ensuite, ni depuis Suwayomi, ni depuis une restauration (EF-BIB-052, EF-BIB-053).

## Lancer une purge

    shelfhold purge --now --unit <id> --confirm <id>
    shelfhold purge --now --work <id> --confirm <id>

(routes `POST /api/v1/units/{id}/purge` et `POST /api/v1/works/{id}/purge`)

- Action **réservée** : administrateur, mot de passe saisi il y a moins de 15 minutes, ou ligne de
  commande locale. Une clé d'API est refusée.
- **Confirmation tapée** : l'identifiant de l'Unité ou de l'Œuvre, toujours, même pour du contenu
  remplaçable. Le refus a lieu avant toute écriture, pierre tombale comprise.
- Une ligne d'audit est écrite, sans titre.

## Dans quel ordre

1. la **pierre tombale**, durable et chaînée, avant toute suppression ;
2. le **service connecté** : la revendication de Shelfhold sur le téléchargement est retirée chez
   Suwayomi (une demande encore en file est annulée), une seule fois ;
3. l'**index de `/store`** (`INDEX.jsonl`) est réécrit **avant** les copies : un arrêt brutal laisse un index
   qui cite des fichiers présents, jamais une ligne morte ;
4. les **copies** : `/library`, `/store`, la corbeille, les vignettes et caches dérivés, `/data` et le miroir
   de provenance ;
5. pour une **Œuvre**, le **désabonnement** de Suwayomi (`unfollow`), quand tous les téléchargements sont
   réglés ;
6. la **clôture** : les lignes de base (variantes, contenus, copies, exports, positions de lecture), les
   tâches et événements terminés dont le texte cite un identifiant de la purge ou un titre, leurs
   notifications en attente, les journaux d'intentions et les plans d'export terminés dont les chemins
   épellent le titre, et pour une Œuvre ses métadonnées et son `work.json`.

Le dossier de série vidé reste. Un fichier qui porte d'autres Unités (volume, archive multi-chapitres) n'est
jamais effacé pour une seule : la purge s'arrête (`purge.collateral`) et le rapport le dit. Une copie sur une
racine en lecture seule (`adopt_ro`) n'est jamais touchée. Une racine absente : la purge fait ce qu'elle
peut, garde les lignes de journal dues, et la même commande, relancée au remontage, termine.

## Ce qui reste chez Suwayomi

Si Suwayomi garde le fichier pour un autre compte (mode `ui_login`), Shelfhold ne le supprime jamais : le
rapport dit `kept_by_other_account` et l'alerte vous demande de le supprimer dans Suwayomi. Une revendication
dont le résultat est inconnu après un arrêt (`upstream_unresolved`) n'est **jamais rejouée** : regardez-la
avec `shelfhold intents list`, puis `shelfhold intents resolve <intention>`.

## Le journal de la base : `--checkpoint`

La base est en mode journal (WAL) : un titre effacé d'une table peut survivre un moment dans le fichier du
journal. Shelfhold active `secure_delete` sur chaque connexion et vide le journal (`wal_checkpoint(TRUNCATE)`)
à la fin d'une purge. Si un lecteur de la base garde un instantané ouvert (le serveur, quand la purge est
lancée depuis l'hôte), le journal ne peut pas être vidé : trois essais espacés d'une seconde, puis
`purge.wal_busy`. **L'effacement est fait**, seul le vidage manque. Relancez quand le serveur est inactif,
ou arrêtez-le ; sans rien faire, le journal est vidé chaque nuit et à l'arrêt. Pour ne refaire que ce vidage :

    shelfhold purge --checkpoint

## Après une mise à jour depuis la 0.2 : `--leftovers`

Les purges de la version précédente n'ont pas libéré les téléchargements chez Suwayomi et ont laissé des
titres dans les pages libres de la base. Rien n'est appliqué d'office :

    shelfhold purge --leftovers                 # le plan, et sa version
    shelfhold purge --leftovers --confirm <version>

Le plan dit ce que le service tient encore. L'application est une action réservée ; elle commence par un
`VACUUM` qui réécrit la base entière (prévoyez de la place libre, les écritures attendent), refusé avec
`purge.busy` si une sauvegarde ou l'entretien nocturne le tient. Une version de plan périmée ne fait rien
(`plan.stale`). Une fois les téléchargements supprimés à la main dans Suwayomi, `shelfhold purge --kept-ack`
efface la marque qui les signalait.

## Ce que Shelfhold ne peut pas effacer

Chaque rapport de purge le rappelle : les sauvegardes de l'hôte, les caches et signets de Kavita, les copies
faites hors de Shelfhold, les journaux de l'hôte, les sauvegardes retenues. Pour les sauvegardes intégrées,
le rapport **liste** celles qui sont encore présentes et datent d'après la création des lignes purgées (leur
nom, c'est-à-dire un horodatage : elles ne sont ni déchiffrées ni réécrites). Elles contiennent encore l'Œuvre jusqu'à ce
que la rétention les renouvelle ([sauvegarde.md](sauvegarde.md)), et une restauration ne la
ressuscite pas : le journal des pierres tombales est rejoué avant toute réadoption.

## Les pierres tombales

Avant de supprimer quoi que ce soit, la purge écrit une **pierre tombale** : l'enregistrement de
ce qui ne doit pas revenir. Elle est dans la base **et** dans un journal par racine
(`.shelfhold/tombstones`), en ajout seul, chiffré par la clé du trousseau. Elle ne contient aucune
empreinte en clair. Elle vise une variante précise, une place (série et numéro), la liaison d'une
série à sa source ou une Œuvre entière.

Conséquences :

- Un chapitre purgé n'est pas retéléchargé par le suivi, une série purgée n'est pas resuivie
  depuis la bibliothèque amont, un fichier dont l'empreinte est une pierre est refusé à la réception
  et à l'adoption.
- Une restauration de sauvegarde ne ressuscite pas une purge : le journal des racines est rejoué
  **avant** toute réadoption ([restauration.md](restauration.md)).
- **Racine absente** : la purge fait ce qu'elle peut ; les lignes de journal dues à la racine
  démontée sont gardées en base, et toute écriture vers cette racine est refusée tant qu'elles ne
  sont pas écrites. Relancez la même commande au remontage : elle termine.
- **Le trousseau compte** : sans la clé d'une pierre, Shelfhold ne peut plus dire ce qu'elle
  interdit ; il bloque alors la réacquisition de son périmètre. Gardez l'export de clé
  ([restauration.md](restauration.md)).

Lister ou lever une pierre tombale sont aussi des actions réservées ; aucune commande ne les
expose dans cette version. Les commandes de sortie qui touchent aux pierres tombales
(`tombstone flush`, `set-aside-journal`, `rekey`, `accept-lost-keyring`) sont dans
[etats-attente-et-sorties.md](etats-attente-et-sorties.md) et [secrets.md](secrets.md).
