# Récupérer de l'espace : la corbeille et la levée d'une pierre tombale (EF-BIB-030)

Un fichier supprimé va dans la corbeille de sa racine (`.shelfhold/recycle/<entrée>`) pendant
30 jours. Trois choses rendent cet espace plus tôt, et une quatrième lève un blocage.

## « Vider la corbeille maintenant »

    shelfhold trash empty --confirm empty-trash

(route `POST /api/v1/trash/empty`, corps `{"typed": "empty-trash"}`)

- Action **réservée** : administrateur, mot de passe saisi il y a moins de 15 minutes, ou ligne de
  commande locale ; une clé d'API ou un jeton d'appareil est refusé. La phrase tapée est comparée par
  le serveur, quelle que soit la porte. Une ligne d'audit (`trash.emptied`) porte le nombre
  d'entrées, jamais un titre.
- Seules les corbeilles des racines sont vidées, et seulement les entrées que Shelfhold y a rangées.
  Un fichier à vous, un lien symbolique (jamais suivi), le dossier réservé `.shelfhold/` (sentinelle,
  journaux, bail) ne sont pas touchés.
- Restent, et sont comptées dans la réponse : l'entrée d'une Unité devenue **irremplaçable**, une
  entrée qu'une opération en cours nomme (une restauration, une montée de version), une copie qu'une
  sauvegarde retenue peut encore chercher, un original adopté sans copie vérifiée dans `/store`.
- Deux vidages en même temps : chaque entrée n'est effacée qu'une fois, aucun ne s'arrête sur
  l'autre.

## Purge anticipée sous pression disque

Quand un volume passe sous son seuil (`max(5 %, 10 Gio)` de libre, garde disque), une tâche (toutes
les 10 minutes) vide la corbeille de ce volume, **la plus ancienne d'abord**, et s'arrête dès que le
volume remonte au seuil de reprise. Garde-fous :

- jamais une entrée **de moins de 7 jours** (fsops lit l'âge sur le disque), jamais l'entrée d'une
  Unité irremplaçable, jamais ce que retiennent une opération en cours ou une sauvegarde ;
- au plus 50 entrées par passage, chacune sous une intention ;
- une ligne d'audit (`trash.early_purged`, acteur `system`) par passage qui a retiré quelque chose.

C'est une **exception** à « la corbeille garde 30 jours » : elle est signalée dans la page Santé
(ENF-OPS-001).

## Un ajout au-delà de l'espace libre est refusé

La feuille « Ajouter » compare la taille estimée à l'espace libre moins la réserve (le seuil de
pause de la garde disque) : `POST /api/v1/library/space-check` répond 409
`library.space-insufficient` avec `estimated_bytes`, `free_bytes`, `reserve_bytes` et l'action
`empty_trash`, **avant toute écriture**.

Ce contrôle n'est pas seulement une route à part : chaque porte qui écrit dans `/data` ou `/store`
le refait **côté serveur** avant sa première écriture, avec les mêmes chiffres et le même 409 :
l'acquisition (préparation et mise en file du téléchargement), l'approbation d'une demande (qui
passe par les mêmes portes), le suivi d'une série, `backfill` (sur sa propre estimation) et
l'import d'un dossier ou d'un arbre (la simulation `--dry-run` n'écrit rien et reste permise).
Quand la taille est inconnue, l'ajout est compté pour un octet : il passe tant qu'il reste de la
place au-dessus de la réserve, et il est refusé quand le volume est déjà à la réserve. La tâche
d'import programmée attend alors que la place revienne (`space_insufficient`), elle n'échoue pas.

Une entrée de corbeille que la base ne connaît pas (base restaurée d'avant la suppression) n'est
ni vidée ni purgée par anticipation : elle est comptée parmi les entrées gardées, et le balayage
à 30 jours la retire. La purge anticipée ne touche qu'aux entrées de plus de 7 jours.

## Lever une pierre tombale

Pour choisir le numéro :

    shelfhold tombstone list

(route `GET /api/v1/tombstones`, administrateur) affiche, pour chaque pierre qui bloque encore, son
numéro, son jour, sa portée et son motif. Jamais le titre ni le chemin d'une œuvre effacée : la
table ne les garde pas, et la liste ne les rouvre pas. Une purge pose plusieurs pierres (une par
identité effacée) : chacune se lève par son propre numéro.

    shelfhold tombstone lift --seq <n> --confirm "lift <n>"

(route `POST /api/v1/tombstones/{n}/lift`, corps `{"typed": "lift <n>"}`)

- Action **réservée**, comme `purge --now`. L'audit (`tombstone.lifted`) porte l'acteur et le
  numéro, **jamais une valeur ni un titre**.
- La levée est une **ligne de plus** dans la table et dans chaque journal (le journal ne s'efface
  pas) : elle survit à un redémarrage et à une restauration de `/data`. Une racine absente la reçoit à
  son retour.
- Elle ne **restaure rien** : aucune Unité, aucun fichier, aucune demande. Elle ouvre seulement la
  porte : la pierre levée ne bloque plus la réacquisition, l'adoption, l'import ni une migration.
  Une nouvelle purge du même contenu pose une nouvelle pierre, qui bloque de nouveau.
- Comme les autres purges, elle invalide les jetons `?since=` : les clients se resynchronisent.
