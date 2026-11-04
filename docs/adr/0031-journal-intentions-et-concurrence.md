---
status: accepté
date: 2026-11-02
amended: 2026-11-03 (passe 5) ; 2026-11-03 (passe 7) ; 2026-11-03 (passe 12)
decision-makers: mainteneur
---
# ADR-0031 — Journal d'intentions, verrous de ressource et instance unique

## Contexte et problème
La spécification de la passe 2 garantit l'atomicité d'**une** écriture (staging, `fsync`, `renameat`, EF-EXP-006), mais pas celle des opérations en plusieurs étapes : plan de renommages (EF-EXP-030), upgrade au même chemin avec envoi en corbeille (EF-EXP-016), suppression vers la corbeille (EF-BIB-021), purge Suwayomi (B6). Un arrêt brutal entre `renameat` et le `COMMIT` laisse le manifeste pointer vers l'ancien chemin ; la vérification voit alors « manquant » + « inconnu » et, si le fichier a moins de 24 h, le **réexporte** : doublon dans Kavita, voire perte du dernier exemplaire en cas de purge (GAP-3-02).
Rien n'empêche non plus deux jobs de modifier la même série en même temps (export d'un nouveau chapitre pendant un ré-étagement), ni d'appliquer un aperçu devenu périmé (GAP-3-03). `lock_task` de Huey verrouille une **tâche**, pas une **ressource**. Enfin, l'hypothèse « une seule instance » (`flush_locks` et libération des baux au démarrage) n'est pas imposée : une pile de test ou un redéploiement qui se chevauche crée deux écrivains sur `/library` (GAP-3-04).

## Facteurs de décision
- Zéro doublon et zéro perte après un `kill -9` à n'importe quel point.
- Solution locale (SQLite + système de fichiers), sans service supplémentaire.
- Doit être en place **avant** que le manifeste se remplisse (M1).
- Lisible et rejouable par un mainteneur seul.

## Options considérées
### A. Réconciliation a posteriori seule (situation passe 2)
- Avantages : rien à ajouter ; la vérification complète déjà le manifeste quand le hash correspond.
- Inconvénients : ne sait pas distinguer « déplacé par Shelfhold puis crash » de « supprimé à la main » ; réexport automatique < 24 h → doublon ; purge interrompue → perte possible.
### B. Journal d'intentions + verrous de ressource + plans versionnés + instance unique (`flock` + bail)
- Avantages : chaque opération multi-étapes est décrite avant d'être faite et peut être achevée ou annulée de façon idempotente ; sérialisation par Œuvre et par dossier de série ; plan périmé détecté ; seconde instance refusée.
- Inconvénients : une table et un passage de démarrage de plus ; discipline de code (toute opération disque passe par l'API `fsops` + intention).
### C. Système de fichiers transactionnel (snapshots ZFS/btrfs autour de chaque lot)
- Avantages : atomicité forte.
- Inconvénients : dépend du système de fichiers de l'utilisateur ; impossible sur ext4, NFS, SMB ; hors de portée d'un conteneur sans privilèges.

## Décision
Option B (arbitrages C2, C3, C4).
- **Journal d'intentions** (`fs_intent` : op, chemins source et cible, hash et `(dev, ino)` attendus, état) écrit et validé **avant** toute opération multi-étapes (renommage, upgrade, suppression, purge, mise à l'abri, nettoyage de dossiers) ; au démarrage, **avant tout job**, chaque intention ouverte est achevée ou annulée ; la vérification du rangement ne réexporte jamais un élément sous intention ouverte (EF-EXP-041).
- **Verrous de ressource** par Œuvre puis par chemin de série cible, pris dans cet ordre par tout job qui modifie le disque ; **plans versionnés** (`base_version` + empreinte des fichiers) ; application d'un plan périmé refusée avec « l'aperçu a changé, recalculer » (EF-EXP-042).
- **Instance unique** : `flock` exclusif sur `/data/.lock` et bail horodaté dans `/library/.shelfhold/lease` renouvelé et vérifié avant chaque écriture ; une seconde instance refuse de démarrer en écriture et l'explique (ENF-OPS-015). `flush_locks` et la libération des baux n'ont lieu qu'après la prise de ce verrou.
- La détection d'un **autre écrivain** (EF-EXP-043) s'appuie sur le même registre : tout changement dans un dossier géré qui n'est ni au manifeste ni sous intention vient d'un tiers.

## Conséquences
- Positives : invariants I9 et I10 (`02-architecture.md` §3.1 bis) testables par propriétés ; le test « kill -9 à chaque étape d'un plan de 10 renommages → 0 doublon, 0 perte » devient possible ; base saine pour le fan-out M4.
- Négatives : démarrage un peu plus long quand des intentions sont ouvertes ; tout nouveau type d'opération disque doit définir sa résolution (achever/annuler).

## Risque principal
Une intention dont la résolution est ambiguë (fichier ni à la source ni à la cible). Mitigation : jamais de réexport automatique, `reconcile.drift` et question à l'admin ; la sentinelle et le bail bloquent les écritures si le montage n'est pas le bon.

## Réversibilité
Moyenne : la table et les verrous sont internes ; les retirer ne change pas les formats sur disque, mais ferait perdre les garanties.

## Amendement passe 4 (2026-11-03)
(D13, GAP-4-09, GAP-4-07) : le bail d'instance n'est **pris qu'à l'ouverture d'une session d'écriture** (battement de cœur pendant le lot, libéré à la fin) ; hors session, le `flock` sur `/data/.lock` protège l'instance et rien n'est écrit dans `/library`, ce qui laisse les disques se mettre en veille (ENF-OPS-014). La détection d'un autre écrivain compare aussi la **taille et le mtime** (modification sur place). Un plan périmé renvoie 412 (ADR-0006). Les garanties de cohérence sont en **M1a** et ne sont jamais coupables (ADR-0035).

## Amendement passe 5 (2026-11-03)
GAP-5-16, E2. `shelfhold migrate` refuse de tourner tant qu'il reste des intentions ouvertes ; le schéma de `fs_intent` est gelé et versionné (`intent_schema_version`) et une intention d'un format inconnu n'est jamais rejouée (ENF-OPS-019). Nouvelle opération `repatriate` (copie vérifiée par hash au même chemin relatif, EF-EXP-051).

## Amendement passe 7 (2026-11-03)
G4, G7 (GAP-7-04, GAP-7-07).
- Une intention créée avant `instance.restore_epoch` (venue d'un instantané) n'est **jamais rejouée** : elle passe `stale_from_backup` et une réconciliation suit (ADR-0044, EF-INT-026, invariant I28).
- Le journal d'intentions ne porte que des **références** : les relevés durables (réglages amont relevés à l'adoption) vivent dans `upstream_snapshot`, jamais dans `fs_intent` (EF-EXP-056).
- Le bail est pris par racine physique (ENF-OPS-015).

## Exigences liées
EF-EXP-006, EF-EXP-016, EF-EXP-018, EF-EXP-030, EF-EXP-041, EF-EXP-042, EF-EXP-043, EF-BIB-021, EF-BIB-038, EF-INT-003, ENF-OPS-007, ENF-OPS-015, ENF-QUAL-006, ENF-OPS-014, ENF-OPS-019, EF-EXP-051, EF-INT-026, EF-EXP-056.

## Sources
- https://www.sqlite.org/atomiccommit.html *[mem]*
- https://man7.org/linux/man-pages/man2/flock.2.html *[mem]*
- https://man7.org/linux/man-pages/man2/rename.2.html
- https://huey.readthedocs.io/en/latest/api.html#Huey.lock_task *[mem]*
- `deliverables/review/chasseur-trous-passe3.md` (GAP-3-02, GAP-3-03, GAP-3-04), `deliverables/review/ARBITRAGES-passe3.md` (C2–C4)
- `deliverables/review/chasseur-trous-passe4.md` (GAP-4-07, GAP-4-09)
- https://man7.org/linux/man-pages/man2/flock.2.html
- `deliverables/review/ARBITRAGES-passe4.md`
- `deliverables/review/chasseur-trous-passe5.md`
- `deliverables/review/explorateur-passe5.md`
- `deliverables/review/ARBITRAGES-passe5.md`
- `deliverables/review/chasseur-trous-passe7.md`, `deliverables/review/explorateur-passe7.md`, `deliverables/review/ARBITRAGES-passe7.md` (passe 7)

## Amendement passe 12 (2026-11-03)
M3 (GAP-12-03).

- Le `flock` exclusif sur `/data/.lock` empêche une seconde instance ; la CLI n'agit donc sur une instance en marche que par le **socket Unix** `/data/shelfhold.sock` du serveur (`0600`, `SO_PEERCRED`) ; elle ne prend le `flock` elle-même que si le serveur est arrêté. Aucun canal TCP n'est ouvert pour la CLI (ADR-0049).

Source : `deliverables/review/chasseur-trous-passe12.md`, `deliverables/review/explorateur-passe12.md`, `deliverables/review/ARBITRAGES-passe12.md` (passe 12)
