# Secrets et trousseau : procédure d'administration

Le trousseau (`/data/keyring`, ou `SHELFHOLD_SECRET_KEY_FILE`) contient les clés qui scellent les secrets en base. `shelfhold secrets rotate` et `shelfhold secrets export` modifient ou lisent ce trousseau et prennent pour cela le verrou d'instance `/data/.lock`.

## Rotation de la clé

Une instance qui tourne tient `/data/.lock` : la commande est alors refusée avec le message « arrêtez Shelfhold, puis relancez ». La procédure :

1. arrêter Shelfhold (`docker compose stop shelfhold`) ;
2. lancer la commande sur les mêmes volumes, par exemple `docker compose run --rm shelfhold shelfhold secrets rotate` ;
3. redémarrer Shelfhold.

Une rotation interrompue se reprend en relançant la même commande. Les clés retirées restent dans le trousseau tant qu'un secret en dépend ; `shelfhold secrets rotate --retire <kid>` en retire une, et refuse si elle est active, encore utilisée ou citée par une pierre tombale. `shelfhold secrets status` (lecture seule, `--json` possible) signale un trousseau absent, trop ouvert ou resté dans `/data`.

## Export de la clé maîtresse

Même règle : instance arrêtée, puis `shelfhold secrets export --passphrase-file <fichier> --out <fichier>` (`--out -` écrit sur la sortie standard). Le fichier produit est chiffré au format age ; il est à garder hors de l'hôte. Le fichier de phrase de passe doit appartenir au compte qui lance la commande et n'être accessible qu'à lui (`chmod 600`) ; la même règle vaut pour l'import. Un lien symbolique est refusé (copiez le fichier), de même qu'un fichier de `/proc`, `/sys` ou `/dev`. Seule exception : un secret Docker appartenant à root sous `/run/secrets/` (mode 0444), pourvu que personne d'autre ne puisse l'écrire.

## Import d'un trousseau exporté

`shelfhold secrets import --passphrase-file <fichier> --in <export>` fusionne les clés d'un export dans le trousseau, puis rejoue les pierres tombales en attente. C'est la sortie normale de l'état `tombstone_key_unknown` (une sauvegarde de `/data` antérieure à une rotation, un trousseau qui n'a pas suivi).

- **Action réservée.** Depuis l'hôte (le compte qui atteint `/data`, chemins absolus), ou depuis l'API avec une session dont le mot de passe a été retapé récemment ; une clé d'API est toujours refusée. L'opération est inscrite à l'audit, dans la même transaction que le registre des clés.
- **Route API.** `POST /api/v1/secrets/import` avec `{"passphrase_file": "nom", "in_file": "nom"}` : ce ne sont pas des chemins mais deux **noms simples** (lettres, chiffres, `.`, `_`, `-`) de fichiers du dossier `import/` du dossier de données (`/data/import/`, à créer), ouverts sans suivre les liens. Aucun autre emplacement n'est lisible par l'API.
- **Fusion sans écrasement.** Les clés de l'export qui manquent au trousseau sont ajoutées comme clés retirées ; la clé active en place reste active. Un identifiant de clé déjà présent avec un autre matériel fait refuser l'import entier (le message donne l'identifiant, jamais la clé). Rien n'est supprimé.
- **Trousseau absent.** Si le trousseau est perdu mais que la base existe (le démarrage refuse alors de créer une autre clé), l'import le recrée à partir de l'export, clé active comprise.
- **Fichier forgé.** Un fichier tronqué, au mauvais en-tête, avec un identifiant en double, une clé de mauvaise taille ou trop volumineux (plus de 1 Mio) n'importe rien et ne dit rien de son contenu.
- **Reprise.** Le trousseau est remplacé en une seule étape atomique (fichier synchronisé sur le disque, renommage, dossier synchronisé) : il n'est jamais à moitié écrit. Après une interruption, relancer la même commande termine le travail (registre, audit, rejeu).
- **Pendant `restored_behind`.** L'import est permis, pour sa partie `/data` seulement (trousseau, registre, audit) : une sauvegarde antérieure à une rotation bloque `reconcile --adopt-after-restore` tant que les clés manquent. Aucune pierre tombale n'est rejouée à ce moment (la commande le dit) : c'est la réadoption qui rejoue, avec le trousseau du disque. L'ordre est donc : `secrets import`, puis `reconcile --adopt-after-restore`. Sans export, la sortie est `tombstone accept-lost-keyring` (voir ci-dessous), permise elle aussi pendant le gel.
- **Instance qui tourne.** La commande passe par la socket locale ; instance arrêtée, elle prend le verrou d'instance. La phrase de passe est lue du fichier par le processus qui applique l'import : elle ne passe ni par la ligne de commande, ni par la socket, ni par les journaux.

## Pourquoi arrêter l'instance

Un seul processus à la fois peut écrire sur les volumes (ENF-OPS-015). Le verrou `/data/.lock` est le même pour l'instance, les migrations et le trousseau . Les commandes qui passent par la socket locale (`config`, `target`, `source`, `shelf`, `admin`, `adopt`, `purge`…) n'ont pas besoin de cet arrêt ; celles qui touchent au trousseau, aux migrations ou à la restauration (`secrets export|rotate`, `migrate`, `reconcile`) l'exigent.

## Ce que `shelfhold status` en dit

`shelfhold status` reprend les avertissements du trousseau (les mêmes que `shelfhold secrets status`),
et d'abord celui-ci : **la clé maîtresse vit dans `/data`**. Une copie de `/data` par l'outil de
l'hôte contient alors la clé à côté des secrets qu'elle protège. Placez le trousseau hors de `/data`
avec `SHELFHOLD_SECRET_KEY_FILE` (secret Docker ou volume distinct). L'avertissement ne montre jamais
la clé.

## Trousseau perdu, sans export

`shelfhold tombstone accept-lost-keyring --confirm "<phrase>"` (action réservée : session récente ou hôte, jamais une clé d'API ; audité dans la même transaction) abandonne les clés de pierres tombales qui manquent au trousseau. Les lignes qu'elles protégeaient deviennent **inertes** : du contenu purgé peut revenir à la réacquisition ou à la réadoption. Ce n'est donc qu'un dernier recours, après `secrets import`.

- Hors gel, la phrase est `the keyring is lost`.
- Pendant `restored_behind` (sauvegarde antérieure à une rotation, sans export), la commande est permise parce qu'elle est la seule sortie de `key_unknown`, qui bloque `reconcile --adopt-after-restore`. La phrase énonce alors la conséquence : `the keyring is lost, its tombstones become inert and purged content may come back`. Les journaux sont lus, aucun n'est écrit ; l'ordre est cette commande, puis `reconcile --adopt-after-restore`.
- `shelfhold status` et `shelfhold secrets status` gardent un avertissement durable (`clé(s) de pierres tombales déclarées perdues`) : retrouver le trousseau plus tard n'annule pas la déclaration.

## Clé fuitée : `tombstone rekey`

Si la clé qui scelle les pierres tombales a fuité (une copie du trousseau ou de `/data` est sortie du foyer), un tiers qui lit un journal peut déchiffrer les références purgées. Deux commandes, dans cet ordre :

1. `shelfhold secrets rotate` émet une nouvelle clé active (la clé qui a fuité cesse de sceller quoi que ce soit).
2. `shelfhold tombstone rekey --kid <clé fuitée>` (répétable ; action réservée : hôte ou session récente, jamais une clé d'API ; audité) marque cette clé **compromise** puis scelle de nouveau, sous la clé active, chaque ligne de la base, chaque ligne due à une racine absente et chaque journal. La clé active ne peut pas être désignée.

Ce que la commande garantit :

- **Aucune pierre tombale perdue.** Le journal d'une racine est remplacé par un nouveau fichier, écrit et `fsync`é, puis par un seul `rename` atomique : à chaque instant le journal est l'ancien ou le nouveau, entier. Un arrêt brutal (SIGKILL, coupure) ne laisse jamais un journal vide ou à moitié écrit ; relancer la commande termine le travail. Une pierre tombale posée pendant la réécriture est dans le nouveau fichier.
- **Racine démontée.** Sa réécriture est due : `status` la liste (`tombstone_lines_owed`), aucune autre écriture n'y est acceptée, et elle est faite avant toute autre au remontage (ou par `shelfhold tombstone flush`).
- **L'ancienne clé n'ouvre plus le nouveau journal.** Elle reste au trousseau et au registre (pour lire d'anciennes sauvegardes), marquée compromise ; `shelfhold status` le rappelle. Une sauvegarde faite avant n'est pas rechiffrée : elle reste lisible par qui détient la clé qui a fuité.
- **Un journal tronqué n'est pas réécrit.** Une racine en `tombstone_journal_truncated` garde sa dette de réécriture ; sa sortie est `tombstone set-aside-journal`, qui réécrit le journal depuis la base, déjà rechiffrée.

## Journal tronqué : `tombstone_journal_truncated`

Chaque racine garde, en base et dans sa sentinelle (authentifiée sous le trousseau), le dernier numéro de ligne de son journal et une étiquette chaînée sur toutes les lignes (`H(précédente ‖ numéro ‖ étiquette AEAD)`). Un journal qui ne l'atteint plus, dont on a supprimé les lignes de fin, est détecté au démarrage, après une restauration et à chaque `shelfhold status`, même si la sentinelle a été réécrite pour lui correspondre : la base fait foi, et une sentinelle sans MAC valide n'est pas crue. Réordonner ou dupliquer des lignes ne change rien. Le rapprochement et la réacquisition sont bloqués sur cette racine seulement, jusqu'à `shelfhold tombstone set-aside-journal --root <racine>` (le fichier est gardé sous `tombstones.aside-<date>`, aucune ligne n'est perdue).
