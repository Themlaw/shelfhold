# Secrets et trousseau : procédure d'administration

Le trousseau (`/data/keyring`, ou `SHELFHOLD_SECRET_KEY_FILE`) contient les clés qui scellent les secrets en base. `shelfhold secrets rotate` et `shelfhold secrets export` modifient ou lisent ce trousseau et prennent pour cela le verrou d'instance `/data/.lock`.

## Rotation de la clé

Une instance qui tourne tient `/data/.lock` : la commande est alors refusée avec le message « arrêtez Shelfhold, puis relancez ». La procédure :

1. arrêter Shelfhold (`docker compose stop shelfhold`) ;
2. lancer la commande sur les mêmes volumes, par exemple `docker compose run --rm shelfhold shelfhold secrets rotate` ;
3. redémarrer Shelfhold.

Une rotation interrompue se reprend en relançant la même commande. Les clés retirées restent dans le trousseau tant qu'un secret en dépend ; `shelfhold secrets rotate --retire <kid>` en retire une, et refuse si elle est active, encore utilisée ou citée par une pierre tombale. `shelfhold secrets status` (lecture seule, `--json` possible) signale un trousseau absent, trop ouvert ou resté dans `/data`.

## Export de la clé maîtresse

Même règle : instance arrêtée, puis `shelfhold secrets export --passphrase-file <fichier> --out <fichier>` (`--out -` écrit sur la sortie standard). Le fichier produit est chiffré au format age ; il est à garder hors de l'hôte. L'import d'un trousseau exporté (`secrets import`) n'existe pas dans cette version : on remet le fichier de trousseau d'origine à sa place.

## Pourquoi arrêter l'instance

Un seul processus à la fois peut écrire sur les volumes (ENF-OPS-015). Le verrou `/data/.lock` est le même pour l'instance, les migrations et le trousseau . Les commandes qui passent par la socket locale (`config`, `target`, `source`, `shelf`, `admin`, `adopt`, `purge`…) n'ont pas besoin de cet arrêt ; celles qui touchent au trousseau, aux migrations ou à la restauration (`secrets export|rotate`, `migrate`, `reconcile`) l'exigent.
