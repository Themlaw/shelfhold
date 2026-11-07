# Secrets et trousseau : procédure d'administration

Le trousseau (`/data/keyring`, ou `SHELFHOLD_SECRET_KEY_FILE`) contient les clés qui scellent les secrets en base. `shelfhold secrets rotate` et `shelfhold secrets export` modifient ou lisent ce trousseau et prennent pour cela le verrou d'instance `/data/.lock`.

## Rotation de la clé

Une instance qui tourne tient `/data/.lock` : la commande est alors refusée avec le message « arrêtez Shelfhold, puis relancez ». La procédure :

1. arrêter Shelfhold (`docker compose stop shelfhold`) ;
2. lancer la commande sur les mêmes volumes, par exemple `docker compose run --rm shelfhold shelfhold secrets rotate` ;
3. redémarrer Shelfhold.

Une rotation interrompue se reprend en relançant la même commande. Les clés retirées restent dans le trousseau tant qu'un secret en dépend ; `shelfhold secrets rotate --retire <kid>` en retire une, et refuse si elle est active, encore utilisée ou citée par une pierre tombale. `shelfhold secrets status` (lecture seule, `--json` possible) signale un trousseau absent, trop ouvert ou resté dans `/data`.

## Export de la clé maîtresse

Même règle : instance arrêtée, puis `shelfhold secrets export --passphrase-file <fichier> --out <fichier>` (`--out -` écrit sur la sortie standard). Le fichier produit est chiffré au format age ; il est à garder hors de l'hôte. Le fichier de phrase de passe doit être lisible par son propriétaire seul (`chmod 600`) ; la même règle vaut pour l'import.

## Import d'un trousseau exporté

`shelfhold secrets import --passphrase-file <fichier> --in <export>` fusionne les clés d'un export dans le trousseau, puis rejoue les pierres tombales en attente. C'est la sortie normale de l'état `tombstone_key_unknown` (une sauvegarde de `/data` antérieure à une rotation, un trousseau qui n'a pas suivi).

- **Action réservée.** Depuis l'hôte (le compte qui atteint `/data`), ou depuis l'API avec une session dont le mot de passe a été retapé récemment (`POST /api/v1/secrets/import`, deux chemins de fichiers lus **sur l'hôte du serveur**) ; une clé d'API est toujours refusée. L'opération est inscrite à l'audit, dans la même transaction que le registre des clés.
- **Fusion sans écrasement.** Les clés de l'export qui manquent au trousseau sont ajoutées comme clés retirées ; la clé active en place reste active. Un identifiant de clé déjà présent avec un autre matériel fait refuser l'import entier (le message donne l'identifiant, jamais la clé). Rien n'est supprimé.
- **Trousseau absent.** Si le trousseau est perdu mais que la base existe (le démarrage refuse alors de créer une autre clé), l'import le recrée à partir de l'export, clé active comprise.
- **Fichier forgé.** Un fichier tronqué, au mauvais en-tête, avec un identifiant en double, une clé de mauvaise taille ou trop volumineux (plus de 1 Mio) n'importe rien et ne dit rien de son contenu.
- **Reprise.** Le trousseau est remplacé en une seule étape atomique (fichier synchronisé sur le disque, renommage, dossier synchronisé) : il n'est jamais à moitié écrit. Après une interruption, relancer la même commande termine le travail (registre, audit, rejeu).
- **Pendant `restored_behind`.** L'import est refusé comme toute écriture ; la sortie est `shelfhold reconcile --adopt-after-restore`, puis l'import.
- **Instance qui tourne.** La commande passe par la socket locale ; instance arrêtée, elle prend le verrou d'instance. La phrase de passe est lue du fichier par le processus qui applique l'import : elle ne passe ni par la ligne de commande, ni par la socket, ni par les journaux.

## Pourquoi arrêter l'instance

Un seul processus à la fois peut écrire sur les volumes (ENF-OPS-015). Le verrou `/data/.lock` est le même pour l'instance, les migrations et le trousseau . Les commandes qui passent par la socket locale (`config`, `target`, `source`, `shelf`, `admin`, `adopt`, `purge`…) n'ont pas besoin de cet arrêt ; celles qui touchent au trousseau, aux migrations ou à la restauration (`secrets export|rotate`, `migrate`, `reconcile`) l'exigent.

## Ce que `shelfhold status` en dit

`shelfhold status` reprend les avertissements du trousseau (les mêmes que `shelfhold secrets status`),
et d'abord celui-ci : **la clé maîtresse vit dans `/data`**. Une copie de `/data` par l'outil de
l'hôte contient alors la clé à côté des secrets qu'elle protège. Placez le trousseau hors de `/data`
avec `SHELFHOLD_SECRET_KEY_FILE` (secret Docker ou volume distinct). L'avertissement ne montre jamais
la clé.
