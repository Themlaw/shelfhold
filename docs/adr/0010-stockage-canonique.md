---
status: accepté
date: 2026-11-02
amended: 2026-11-02 (passe 1) ; 2026-11-02 (passe 2) ; 2026-11-03 (passe 6) ; 2026-11-03 (passe 7)
decision-makers: mainteneur
---
# ADR-0010 — Stockage canonique par Unité ; blobs adressés par contenu réservés (M4)

> **Amendée le 2026-11-02 (passes 1 et 2).** En cas de divergence, la section « Amendement passe 1 » prévaut sur le texte d'origine, conservé pour l'historique.

## Contexte et problème
Le store est la source de vérité, à partir de laquelle on produit N exports (Kavita, dossier natif, Kobo…) et des renditions dérivées. Il doit préserver les octets d'origine, permettre la vérification d'intégrité et éviter le double stockage quand c'est possible.

## Facteurs de décision
- Ne jamais réencoder (qualité, hash stables, dédoublonnage).
- Accès aléatoire aux pages pour le lecteur.
- Comptage d'inodes raisonnable.
- Écriture atomique et liens de fichiers possibles vers `/exports`.

## Options considérées
### A. Par Unité : CBZ `ZIP_STORED` + `unit.json` ; paquet XHTML assaini (nh3) + assets pour le texte ; fichier original pour les FileAsset ; blobs `sha256/ab/cd/<hash>` pour couvertures, fichiers et renditions ; refcount + GC
- Avantages : un fichier par chapitre (inodes maîtrisés) ; accès aléatoire peu coûteux dans un ZIP stocké ; hash vérifiables ; renditions dérivées clés au hash source (D35).
- Inconvénients : reconstruire le CBZ si une page est réparée.
### B. Pages en fichiers individuels (arbre `store/works/.../units/.../0001.webp`)
- Avantages : réparation page par page triviale.
- Inconvénients : explosion d'inodes ; export toujours reconstruit.
### C. CAS par page
- Avantages : dédoublonnage maximal.
- Inconvénients : surestimé (seules les pages de crédits scanlator se répètent) ; multiplie les inodes.

## Décision
Option A (D14, D35). Disposition sous `/data` (détails dans `02-architecture.md`) ; `.staging` et `.recycle` sous `/exports` (D17).

**Contrainte EXDEV** : `link()` et `rename()` échouent entre deux points de montage, **même s'il s'agit du même système de fichiers** (bind mounts distincts). Conséquences :
1. Le staging d'export doit être sous `/exports` (même montage que les racines de bindings) pour que le `rename()` final soit atomique.
2. Un hardlink store → export n'est possible que si le store est sous le même montage. Pour cela, l'option `SHELFHOLD_STORE_DIR=/exports/.shelfhold/store` est documentée ; par défaut le store est sous `/data`.
3. En pratique, les **sidecars par binding** (ComicInfo embarqué, OPF réécrit) font que l'octet exporté diffère souvent du store : le hardlink s'applique rarement (seulement aux passthroughs inchangés). L'ordre retenu est donc **reflink (btrfs/XFS) > hardlink > copie**, la copie étant le cas courant ; jamais de symlink ; jamais de modification en place d'un fichier hardlinké.

## Conséquences
- Positives : intégrité vérifiable (EF-BIB-012) ; renditions et dédoublonnage par hash ; exports reproductibles.
- Négatives : double stockage probable pour les exports enrichis de sidecars, à documenter ; reflink seulement sur les FS compatibles.

## Risque principal
Double stockage sur un homelab à disque limité (R11). Mitigation : option store sous `/exports`, reflink, rétention V2 (EF-BIB-018).

## Réversibilité
Moyenne : changer de disposition demande une migration hors ligne du store, outillable car toutes les références passent par la base.

## Amendement passe 1 (2026-11-02)
- **Pas de CAS ni de refcount en V1** (A11, SIM) : stockage **par Unité** = CBZ (ou EPUB) **tel que reçu** + `unit.json`. Les blobs adressés par contenu, le refcount et le GC sont **réservés à M4**, si le fan-out multi-destinations le justifie ; EF-BIB-007 est retirée et devient cette réservation. Les couvertures et vignettes restent adressées par hash (EF-BIB-020).
- **Désembrouillage** (A8, EF-BIB-006) : on conserve les octets reçus **et** on autorise un dérivé canonique déclaré par un Processor « descramble » (`original_sha256` + `derived_sha256`) ; réencodage seulement s'il est exigé, PNG sans perte par défaut.
- **Export par copie** par défaut ; hardlink/reflink en option seulement sur le même système de fichiers et le même montage ; jamais de symlink (EF-EXP-015).
- **Staging et corbeille** : sous le montage de la bibliothèque, `/library/.shelfhold/` (et non plus `/exports`), voir ADR-0022 ; l'option `SHELFHOLD_STORE_DIR` sous ce montage reste documentée.
- **Écritures confinées** : `openat2(RESOLVE_BENEATH|RESOLVE_NO_SYMLINKS)` (repli `O_NOFOLLOW` + vérification `dev/ino`), liens symboliques refusés dans les arbres gérés (ENF-SEC-012, A27).
- **Archives** : jamais d'extraction ; lecture en flux via `safe_open_archive()` avec plafonds (ENF-SEC-002).

## Amendement passe 2 (2026-11-02)
- **Amendée par ADR-0028.** Le stockage par Unité n'est plus systématique : en **mode passage** (défaut des services connectés), seuls `unit.json`, les hash et le manifeste sont conservés ; la **copie conservée** est une option par étagère.
- Le magasin quitte `/data` : volume **`/store`** distinct (disque dur possible, même système de fichiers que `/library` pour les liens durs) ; `unit.json` reste dans `/data/meta` pour toute Unité.
- Fichiers adoptés : **référencés, non copiés** (option « intégrer au magasin »).
- Drapeau calculé `irreplaceable` ; suppression impossible sans confirmation ; PAR2 jamais ; BagIt en M4.

## Amendement passe 6 (2026-11-03)
F4 (GAP-6-04), GAP-6-10.
- **Registre des exemplaires** `replica` (Unité ou `export_item` ; emplacement `upstream`, `library`, `store`, `destination`, `adopted_ro` ; `(dev, ino)`, hash, `independent`, `mutable_by_third_party`, `verified_at`) et **fonction unique** `independent_copies(unit)` (EF-BIB-045). Purge amont (I11), propriété `irreplaceable`, « Mettre à l'abri », page Santé et fan-out M4 n'ont plus de requêtes ad hoc : ils appellent tous cette fonction.
- **Source des octets** pour un fan-out ou le lecteur : copie de `/store` si elle existe, sinon le fichier de destination **seulement si son hash correspond au manifeste**, sinon un nouveau téléchargement.
- Une Variante porte **0..N** contenus, chacun avec son `media_type` (EPUB, KEPUB, PDF, CBZ…) ; la destination déclare `accepted_content_types` ordonné et le planificateur choisit (EF-EXP-058) ; `rendition` (M5) ne sert que si aucun format natif ne convient.
- Les liens `covers[]` et `content_coverage` sont remplacés par `unit_coverage` (ADR-0040).

## Amendement passe 7 (2026-11-03)
G1 (GAP-7-01).
- **Deux niveaux d'empreinte** (EF-BIB-048, ADR-0043) : `content_hash` mesure la charge utile d'origine normalisée (fonction unique versionnée), `file_hash` le fichier rendu pour une destination ; `replica`, le manifeste et `unit.json` portent les deux ; `independent_copies()` compte sur `content_hash` (invariant I25).
- **`/store` garde toujours l'original tel que reçu**, jamais un rendu de destination. Si la source a disparu, la mise à l'abri extrait l'original du fichier de `/library` (ComicInfo et fichiers annexes de Shelfhold retirés, OPF d'origine restauré) et vérifie son `content_hash` avant de l'écrire (`store_provenance = extracted`) ; un écart n'écrit rien.
- La règle « source des octets » de la passe 6 compare le `file_hash` de la destination au manifeste, puis le `content_hash` avant toute réutilisation ; le rendu est refait pour chaque destination à partir du canonique.

## Amendement passe 9 (2026-11-03)
J1 (X1).
- `store_provenance` prend trois valeurs : `received` (original reçu), `extracted` (original extrait d'un fichier rendu et vérifié par `content_hash`), **`extracted_derived`** (fichier récupéré depuis une destination alors qu'il s'agissait du **dérivé neutralisé** d'un EPUB `untrusted`, vérifié par `derived_hash` ; jamais présenté comme original, ADR-0047).
- Le dérivé neutralisé est un **dérivé canonique déclaré** (`derived_hash`, `derived_by = xhtml-neutralizer@vN`), au même titre que le désembrouillage ; `/store`, quand il existe, garde l'original.

## Exigences liées
EF-BIB-006, EF-BIB-007, EF-BIB-012, EF-BIB-015, EF-SRC-015, EF-EXP-006, EF-EXP-015, ENF-SEC-002, ENF-SEC-003, ENF-SEC-012, EF-BIB-045, EF-BIB-044, EF-EXP-058, EF-BIB-048, EF-BIB-038, EF-BIB-051.

## Sources
- https://github.com/anansi-project/comicinfo
- https://github.com/paperless-ngx/paperless-ngx
- https://wiki.servarr.com/readarr/settings
- https://github.com/crocodilestick/Calibre-Web-Automated
- https://docs.python.org/3/library/zipfile.html#decompression-pitfalls
- https://man7.org/linux/man-pages/man2/openat2.2.html
- `deliverables/review/chasseur-trous-passe7.md`, `deliverables/review/explorateur-passe7.md`, `deliverables/review/ARBITRAGES-passe7.md` (passe 7)
- `deliverables/review/chasseur-trous-passe9.md`, `deliverables/review/explorateur-passe9.md`, `deliverables/review/ARBITRAGES-passe9.md` (passe 9)
