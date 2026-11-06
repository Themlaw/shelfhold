# Migrer depuis des scripts : passer d'une automatisation existante à Shelfhold

Beaucoup d'installations nourrissent Kavita directement : le dossier de téléchargement de Suwayomi
est monté dans Kavita, et des tâches planifiées renomment, dédoublonnent ou posent les couvertures.
Shelfhold ne cohabite pas avec cela sur les mêmes dossiers. Quand il reconnaît une série nourrie
ainsi (voir ci-dessous), il **refuse d'activer le suivi** de l'étagère tant que vous n'avez pas
confirmé chaque étape de cette procédure (EF-EXP-044, ADR-0034). Rien n'est écrit, copié ni
déplacé par la détection.

## Comment Shelfhold reconnaît une série nourrie par Suwayomi

Ce sont des indices montrés comme tels, jamais des preuves : des fichiers nommés
`<équipe>_<chapitre>.cbz` (au moins la moitié des archives du dossier) et, dans les ComicInfo des
cinq premières archives, les champs `Translator` ou `Web` ou une note qui cite Suwayomi ou
Tachidesk. Un dossier de téléchargement de Suwayomi qui sort de `/library` est aussi signalé.

## Procédure

Faites une sauvegarde d'abord ([restauration.md](restauration.md)). Chaque étape se confirme ;
la confirmation porte sur l'empreinte du rapport de détection que vous avez lu et vaut pour toute
la destination.

1. **Adopter** la bibliothèque existante : [adopter-kavita.md](adopter-kavita.md).
2. **Apparier** les séries adoptées au suivi Suwayomi (relire chaque proposition, surtout les
   cas ambigus). Aucune série n'est jamais liée deux fois.
3. **Rapatrier** les fichiers adoptés qui vivent hors de `/library` (le volume de Suwayomi monté
   dans Kavita) : [rapatrier-avant-demontage.md](rapatrier-avant-demontage.md). Si le dossier de
   téléchargement de Suwayomi est déjà un chemin sous `/library`, changez seulement ce dossier
   dans Suwayomi : les fichiers restent en place, il n'y a rien à copier.
4. **Retirer les montages directs** : dans le compose de Suwayomi (et celui de Kavita), enlevez les
   montages qui font écrire Suwayomi dans les dossiers de Kavita. Suwayomi garde son propre dossier
   de téléchargement, que Shelfhold lit par l'API de Suwayomi (mode passage). Redéployez.
5. **Désactiver les scripts** de renommage, de dédoublonnage et de couvertures pour les séries que
   Shelfhold gère. Gardez, si vous en avez un, le script de mise à jour des extensions de Suwayomi
   tant que la sonde fonctionnelle des sources n'existe pas.
6. **Confirmer la bascule** : `shelfhold adopt --confirm-migration --destination <id>` affiche le
   rapport, l'empreinte et les cinq étapes que vous attestez (`adopted`, `pairing_reviewed`,
   `repatriated`, `direct_mounts_removed`, `scripts_disabled`), puis
   `shelfhold adopt --confirm-migration --destination <id> --confirm <empreinte>` les atteste d'un
   coup, pour le rapport que vous avez lu : il n'y a pas de confirmation partielle. Si le rapport de
   détection change ensuite (une série nouvelle), la confirmation est refusée et à refaire.
7. **Choisir le mode de vérification** des sources : `always_on` (défaut, le seul actif en M1a) ;
   `piloted` est accepté mais **inactif avant M1b** et la commande le signale
   ([headless.md](headless.md), clé `source.check_mode`).
8. **Planifier hors de l'hôte** : les redéploiements passent par votre outil d'orchestration
   habituel, sans cron ni timer sur l'hôte pour Shelfhold ([redeploiement.md](redeploiement.md)).

Remplacez aussi le filtrage d'équipes après coup par la préférence d'équipe par série de
Shelfhold, qui choisit avant le téléchargement.

## Revenir en arrière

[retour-arriere.md](retour-arriere.md) : `shelfhold detach --restore-upstream` redonne à Suwayomi
ses réglages d'avant et laisse tous les fichiers où ils sont.
