# Adopter une bibliothèque Kavita existante

L'adoption inscrit dans Shelfhold les fichiers que votre Kavita possède déjà, **sans les
modifier, les renommer ni les déplacer** (EF-EXP-028). Elle ne télécharge rien et ne crée aucun
suivi : le suivi est une étape à part, l'appariement.

## Prérequis

- Une destination déjà créée (`target add kavita`, `shelf create`, voir
  [headless.md](headless.md)) et une bibliothèque Kavita de type Manga, **réservée aux adultes**.
  Si un compte restreint à des profils d'enfants peut voir la bibliothèque, l'adoption est refusée
  (`target.child_exposure`) avec la liste des comptes concernés : retirez-leur l'accès dans Kavita.
- Instance non gelée : si `shelfhold status` annonce `restored_behind`, sortez-en d'abord
  ([restauration.md](restauration.md)).
- Une sauvegarde récente ([restauration.md](restauration.md)).

## Procédure

```sh
shelfhold adopt --destination <id> --dry-run
```

L'aperçu ne touche ni la base, ni le disque, ni Kavita. Il liste les séries, les chapitres lus
depuis les noms de fichiers (par l'analyseur de Kavita lui-même), les doublons (deux fichiers du
même numéro : tous deux inscrits, aucun supprimé), les fichiers dont le nom différerait des règles
de Shelfhold (signalés, **jamais renommés**), les âges suggérés par les ComicInfo, et une
**empreinte**. Relisez, puis :

```sh
shelfhold adopt --destination <id> --confirm <empreinte>
```

Si quoi que ce soit a changé depuis l'aperçu, la confirmation est refusée
(`adopt.preview_stale`) : refaites l'aperçu. Sans `--dry-run` ni `--confirm`, rien n'est lu.

## Ce que l'adoption écrit

- En base : un enregistrement `adopted` par fichier (chemin relatif, empreinte, taille, date), et
  l'état de la série dans Kavita (identifiant, nom, `SeriesSort`, `LocalizedSeries`, langue) pour
  que les chapitres suivants rejoignent **le même dossier et la même série**.
- Dans Kavita, **une seule écriture** : le motif `**/.shelfhold/**` ajouté aux exclusions de la
  bibliothèque (les vôtres sont conservées). Les réglages d'avant sont gardés pour
  [retour-arriere.md](retour-arriere.md).
- Les âges lus dans les ComicInfo restent des **suggestions** à valider ; l'âge des Œuvres reste vide.
- Un volume sans numéro de chapitre est inscrit comme tel ; aucun chapitre n'est deviné.

## Après l'adoption

1. **Apparier** les séries adoptées au suivi Suwayomi :
   `shelfhold adopt pair [--destination <id>]` propose, pour chaque série, la source de même titre
   normalisé (score de 0 à 1) ; vous confirmez avec `adopt pair --series <id> --source-key <clé>
   --remote-id <id>` (la destination se devine s'il n'y en a qu'une). Les cas ambigus ne sont jamais tranchés seuls.
   `shelfhold adopt --pair-auto` (puis `--confirm <empreinte>`) confirme en lot les propositions sans ambiguïté, d'après un rapport et son
   empreinte ; `--min-score <0 à 1>` fixe le score minimal d'une proposition en lot (0,9 par défaut).
   `adopt pair --follow-from <chapitre>` choisit le premier chapitre à suivre (par défaut, le suivant
   du dernier chapitre adopté) ; si le rattrapage dépasse 100 chapitres, la commande demande de
   redonner le nombre vu avec `--confirm-backfill <nombre>` (`backfill.confirmation_required`).
2. Le suivi part du **plus haut chapitre adopté** : si vous avez les chapitres 1 à 50, la première
   demande est le 51. Pour une série en volumes seuls, aucun filigrane ne se devine :
   `shelfhold adopt set-follow-from <série> <chapitre>` fixe le premier chapitre voulu (état `coverage_unknown`,
   [etats-attente-et-sorties.md](etats-attente-et-sorties.md)).
3. Si Suwayomi alimente aujourd'hui les dossiers de Kavita, l'appariement est refusé tant que
   [migration-depuis-scripts.md](migration-depuis-scripts.md) n'est pas confirmée.

Un dossier de série que Shelfhold ne peut pas écrire est refusé avec la correction exacte
(ACL, `group_add` ou `user:` du compose) : appliquez-la, puis relancez.
