# Copie conservée et « Mettre à l'abri » dans `/store`

Une étagère en mode **conserver une copie** (`retention_mode = keep`) garde, pour chaque Unité, le
fichier tel que reçu dans `/store`, en plus de celui que Kavita lit dans `/library`. Le mode
passage, par défaut, n'en garde aucune (EF-BIB-027).

## Une copie, jamais un lien

La copie conservée est une **copie indépendante** : un inode à elle, des octets relus et comparés
avant d'être mis en place. Sur un système de fichiers qui partage les blocs (btrfs, XFS avec
reflink), Shelfhold fait un clone (`FICLONE`) : l'espace n'est consommé qu'à la première
modification, mais le fichier reste un fichier à part. Ailleurs, il copie les octets et les vide
sur le disque. Il ne fait **jamais** de lien dur, même si `/library` et `/store` sont sur le même
volume.

## Ce que la page Santé compte

« Avec copie conservée » ne compte que les copies indépendantes :

| Cas | Compte ? |
| --- | --- |
| copie dans `/store`, inode distinct, empreinte sha256 relue identique | oui |
| clone reflink (inode distinct, blocs partagés en copie sur écriture) | oui |
| second nom du même inode (lien dur posé par un tiers) | non : un inode est une copie |
| fichier d'un autre contenu ou abîmé | non |

Le budget disque d'une Unité en copie conservée peut donc aller jusqu'à trois exemplaires
(Suwayomi, `/library`, `/store`) ; un clone reflink ne les consomme qu'en partie.

## Rattrapage après un changement de mode

Passer une étagère du mode passage à la copie conservée propose un rattrapage : la tâche
`store.keep_catchup` copie dans `/store` les Unités déjà rangées. Elle tourne dans la fenêtre de
maintenance (03 h-07 h), par lots de 200 Unités ou 5 Gio au plus, tant que la taille estimée tient
dans l'espace libre de `/store` moins la réserve de la garde disque. Le reste attend la nuit
suivante (événement `unit.safeguard_backlog`). La tâche s'arrête entre deux Unités quand la fenêtre
se ferme et reprend où elle s'est arrêtée ; une copie interrompue par un arrêt brutal est terminée
ou annulée au démarrage suivant, jamais à moitié mise en place.

Un fichier de `/library` que l'export a déjà réécrit (ComicInfo) n'est pas l'original : Shelfhold en
extrait les pages, vérifie l'empreinte du contenu et ne garde jamais un fichier rendu dans `/store`.

## `store/INDEX.jsonl`

Chaque nuit, dans la fenêtre, Shelfhold réécrit `INDEX.jsonl` à la racine de `/store` s'il a changé :
une ligne JSON par fichier conservé (`unit`, `path`, `title`, `lang`, `number`, `sha256`). Si `/data`
est perdu, `/store` reste lisible. Aucune Unité sous pierre tombale n'y figure ; il ne contient ni
secret ni chemin hors de `/store`, et il se régénère sans perte.

## « Mettre à l'abri » : copier avant que la source disparaisse

Même sur une étagère en mode passage, Shelfhold copie dans `/store` ce qui risque de disparaître. La tâche
`library.safeguard` tourne dans la fenêtre de maintenance (03 h 40 par défaut), après le rattrapage de la copie
conservée. Sont dus :

- les Unités dont **l'origine a disparu** (la source ou le service a retiré le chapitre, toutes ses
  Variantes comprises) ;
- les chapitres **candidats** après des erreurs de contenu : une même source amont a rendu au moins deux
  erreurs de contenu de suite (`shelfhold status`, section des candidats). Une panne, un 5xx ou un réveil
  raté ne comptent pas, et une liaison simplement inactive ne déclenche aucune copie : une panne ne provoque
  jamais de copie en masse.

Règles :

- **Copie vérifiée, ou rien.** Le fichier est copié (clone si possible, jamais un lien), puis relu : inode
  distinct, empreinte identique. Une copie qui ne passe pas la vérification est un échec de l'Unité, pas
  une copie. L'ordre est toujours : copie, vérification, puis le reste.
- **Quota et arriéré.** 200 Unités ou 5 Gio par passage, et ce qui tient dans `/store` au-dessus de la réserve
  du garde disque. Le reste est un **arriéré** : état d'attente `safeguard_backlog` dans `shelfhold status`
  (sortie : `shelfhold jobs list`), événement `unit.safeguard_backlog` une fois par exécution. Il se résorbe
  de nuit en nuit.
- **Pierre tombale.** Une Unité couverte par une pierre tombale n'est ni copiée ni touchée.
- **ComicInfo d'origine.** À la réception, celui du fichier est gardé dans `/data/meta/origin/` (1 Mio au
  plus, au mieux : un disque plein coûte la métadonnée, jamais le chapitre) ; il part avec l'Unité en cas de
  purge.

### Effacer chez Suwayomi après un export vérifié (facultatif)

Par défaut, **rien n'est effacé chez Suwayomi**. Pour libérer la place que son téléchargement occupe :

    shelfhold shelf purge-upstream <étagère> --on
    shelfhold shelf purge-upstream <étagère> --off

C'est une action réservée (session récente ou console de l'hôte, jamais une clé d'API), auditée. Un
téléchargement n'est libéré que si **toutes** ces conditions tiennent, dans cet ordre : l'option est active
sur chaque étagère qui détient le contenu ; aucune pierre tombale ne le couvre ; le service n'est pas en
pause (`source_instance_changed`) ; ses rôles sont connus (pas de `roles_unverified`,
[suwayomi-multi-comptes.md](suwayomi-multi-comptes.md)) ; la source est chargée ; la copie lue par Kavita est
**vérifiée** (enregistrement d'export confirmé, et fichier de `/library` relu avec l'empreinte du manifeste) ;
et la copie de `/store` est vérifiée, ou la source est vivante. Une Unité due sans copie de `/store` est
refusée (`safeguard_pending`).

Juste avant l'acte, la copie de `/store` est relue sur le disque : même inode que celui qui a été prouvé,
`st_nlink` égal à 1, empreinte inchangée, aucune copie de `/library` qui en soit le même fichier. Une copie
supprimée, remplacée ou modifiée entre le plan et l'acte annule l'effacement. Un `--off` posé juste avant
l'arrête. En mode `ui_login`, un fichier qu'un autre compte garde n'est jamais supprimé (alerte « supprimer
dans Suwayomi »). Une intention restée en cours après un arrêt n'est jamais rejouée
([purge.md](purge.md), `intents list`).
