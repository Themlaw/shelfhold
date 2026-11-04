---
status: accepté (remplace partiellement ADR-0011, amende ADR-0010)
date: 2026-11-02
amended: 2026-11-02 (passe 2) ; 2026-11-03 (passe 5) ; 2026-11-03 (passe 6) ; 2026-11-03 (passe 7) ; 2026-11-03 (passe 10) ; 2026-11-03 (passe 11) ; 2026-11-03 (passe 12)
decision-makers: mainteneur
---
# ADR-0022 — Montage du parent existant, correspondance de chemins, sentinelle et écritures sous racine

> **Amendée le 2026-11-02 (passe 2).** En cas de divergence, la section « Amendement passe 2 » prévaut sur le texte qui précède.

## Contexte et problème
ADR-0011 imposait un montage unique `/exports`, au même chemin dans Shelfhold et dans Kavita, et la création de nouvelles bibliothèques Kavita. La revue OPS (OBJ-OPS-01, -02) a montré que, chez le propriétaire :
- Kavita lit déjà `/manga`, `/books` et `/comics`, montés depuis `/srv/media/kavita/...` ;
- créer de nouvelles bibliothèques duplique les séries ou fait perdre la progression ;
- la contrainte EXDEV (`rename`/`link` impossibles entre deux montages) ne vaut qu'**à l'intérieur du conteneur Shelfhold** ;
- `chown` est impossible sans `CAP_CHOWN`, que le conteneur durci n'a pas.

Deux autres lacunes ont été relevées :
- un montage oublié ou vide fait croire à la réconciliation que tout a disparu, et elle supprime ou réexporte en masse (GAP-1-04) ;
- les écritures et suppressions sous la racine doivent résister aux liens symboliques plantés (OBJ-SEC-03).

## Facteurs de décision
- Aucune modification du compose de Kavita.
- Opérations atomiques (`rename` dans un même montage).
- Fonctionner sans privilège (uid/gid 1000, `cap_drop: ALL`).
- Ne jamais supprimer sur la foi d'un montage absent.
- Ne jamais écrire hors racine, même en présence de liens symboliques.

## Options considérées
### A. Monter le parent existant + table de correspondance par défaut + sentinelle + `openat2`
- Avantages : zéro changement côté Kavita ; adoption de l'existant possible ; staging et corbeille dans le même montage (pas d'EXDEV) ; protection contre les montages absents ; écritures confinées.
- Inconvénients : une table de chemins à configurer et tester ; dépend d'un noyau ≥ 5.6 pour `openat2` (repli prévu).
### B. Montage unique `/exports` au même chemin (ADR-0011)
- Avantages : pas de correspondance.
- Inconvénients : impose de modifier le compose Kavita et de créer de nouvelles bibliothèques ; incompatible avec l'existant du propriétaire ; « même chemin » rarement respecté par les utilisateurs.
### C. Un montage par bibliothèque Kavita dans Shelfhold
- Avantages : correspondance un pour un.
- Inconvénients : staging et corbeille doivent être dupliqués par montage. Tout déplacement entre bibliothèques devient une copie (EXDEV).

## Décision
Option A.

- **Montage.** On monte le parent existant (ex. `/srv/media/kavita:/library`).
- **Correspondance par défaut.** `SHELFHOLD_PATH_MAP` vaut `/library/manga↔/manga`, `/library/books↔/books`, `/library/comics↔/comics` (EF-EXP-003). Le « même chemin » devient une simple option.
- **Staging et corbeille.** `/library/.shelfhold/staging` et `/library/.shelfhold/recycle`, exclus via les `ExcludePatterns` de Kavita.
- **Propriété.** Pas de `chown`. Les fichiers sont écrits par l'uid/gid du conteneur, avec un `SHELFHOLD_UMASK` configurable et `group_add` si besoin. EF-UX-002 vérifie au démarrage et à chaque nouvelle liaison :
  - que l'uid peut écrire, renommer et créer des dossiers ;
  - qu'une sonde `.shelfhold-probe-<uuid>.epub` est lue par Kavita (`plugin/parse` + scan).

  Si la vérification échoue, l'outil propose le bloc YAML exact à appliquer.
- **Sentinelle.** Un fichier `.shelfhold-mount-id` est posé dans chaque racine gérée (EF-EXP-029). Si elle est absente ou différente, ou si le taux de « manquants » dépasse un seuil, la réconciliation, les suppressions et les scans sont bloqués et une alerte `mount.sentinel_missing` est émise.
- **Écritures sous racine.** Toutes les opérations passent par `openat2(RESOLVE_BENEATH|RESOLVE_NO_SYMLINKS)`, avec un repli `O_NOFOLLOW` + vérification `dev/ino`. Les renommages se font par `renameat` relatif aux descripteurs. Les liens symboliques sont refusés dans les arbres gérés (ENF-SEC-012).
- **Stockage.** Dans le prolongement d'ADR-0010 amendée : pas de CAS ni de refcount en V1. Le stockage se fait par Unité (fichier reçu + `unit.json`). L'export se fait par copie par défaut ; le hardlink ou le reflink sont des options possibles seulement dans le même montage.

## Conséquences
- Positives :
  - adoption de la bibliothèque existante (EF-EXP-028) et progression Kavita préservée ;
  - installation sans toucher Kavita ;
  - protection contre la suppression de masse ;
  - confinement des écritures testé par propriétés.
- Négatives :
  - une table de correspondance à documenter ;
  - les erreurs de configuration se paient en temps de diagnostic, d'où EF-UX-002 et le catalogue d'erreurs ;
  - l'export par copie double le stockage des fichiers exportés.

## Risque principal
Une correspondance fausse : Shelfhold écrit au bon endroit, mais Kavita ne voit rien. Mitigation : la sonde lue par Kavita est obligatoire avant la première écriture réelle, et un plan en mode dégradé est produit si elle échoue.

## Réversibilité
Haute pour la correspondance (configuration). Moyenne pour l'emplacement de la corbeille et du staging (déplacement de fichiers).

## Amendement passe 2 (2026-11-02)
- **Liens par chemin relatif stable** (B11, EF-EXP-039) : liaisons et manifeste sont clés par chemin relatif ; les identifiants Kavita sont un cache ré-résolu ; une réinstallation de Kavita déclenche une re-résolution par chemin, pas une recréation.
- **Seuils chiffrés de la sentinelle** : « manquants » > 5 % ou > 50 fichiers d'une liaison → blocage (GAP-2-16).
- **Volume `/store`** (ADR-0028) : sur le même système de fichiers que `/library` si l'on veut des liens durs ; staging et corbeille restent sous `/library/.shelfhold/`.
- **Détachement** : restaure les réglages Kavita d'avant l'adoption et révoque la clé dédiée (GAP-2-21).
- **Clé Kavita** : `scan-folder` exige une clé d'administrateur ; clé nommée dédiée à expiration (Kavita ≥ 0.9.1 depuis la passe 12 ; 0.8.9 avant), liste blanche des routes (B16).

## Amendement passe 3 (2026-11-02)
- **Sentinelle à génération** (C21) : `.shelfhold-mount-id` porte l'identifiant d'instance et un compteur de génération ; une copie divergente du dossier (ancienne copie sur un autre hôte) est détectée et refusée.
- **Bail d'instance** (C4, ENF-OPS-015) : `/library/.shelfhold/lease` vérifié avant chaque écriture, avec le `flock` sur `/data/.lock` (ADR-0031).
- **Autre écrivain** (C14, EF-EXP-043) : un fichier étranger ou une disparition hors de Shelfhold dans un dossier géré met la série en pause ; jamais de réexport en boucle.
- **Pièges de nommage Kavita** (C17, EF-EXP-045) : titre de chapitre seulement dans ComicInfo, aucun marqueur de saison, jamais deux fichiers de même numéro (adoptés compris), chapitre 0 et couverture, couverture sans préfixe `data:` ; tests golden à noms synthétisés.
- **Plans de renommages** (GAP-3-31) : déplacement atomique d'un dossier de série par lot ; surveillance de dossiers de Kavita lue à l'adoption et signalée.

## Amendement passe 4 (2026-11-03)
(D11, D13, GAP-4-09, GAP-4-10, R5) : (1) le bail `/library/.shelfhold/lease` n'est pris que pendant une **session d'écriture**, pour laisser les disques se mettre en veille (ADR-0031) ; (2) le manifeste enregistre l'**identifiant et la version du gabarit** qui a produit chaque nom de fichier ; (3) un 403 de Kavita met la destination en pause (`target.forbidden`) ; un fichier écrit mais jamais indexé produit `target.not_indexed` ; une réinstallation émet `target.instance_changed`, désormais présent dans le catalogue d'événements.

## Amendement passe 5 (2026-11-03)
GAP-5-05, GAP-5-14, GAP-5-15. Seuil de la sentinelle apprécié **par liaison d'étagère** (`ShelfDestination`), avec plancher : ≥ 10 fichiers manquants **et** > 5 %, ou > 50 fichiers. **Préflight des droits** (création, rename, suppression d'une sonde) dans chaque dossier de série adopté à l'appariement, ligne de matrice `target.permission_denied` et sortie `shelfhold target recheck` (EF-EXP-054). En M1a, la comparaison de hash se fait dans une fenêtre fixe 03 h–07 h. Nom de série figé à l'adoption (EF-EXP-052) ; Kavita reconnu comme second écrivain de métadonnées (EF-EXP-057).

## Amendement passe 6 (2026-11-03)
F5, F6 (GAP-6-05, GAP-6-06, GAP-6-13, GAP-6-26).
- **Racines physiques** : `/library` peut contenir des sous-dossiers qui sont des points de montage distincts. Au démarrage, chaque entrée de `PathMap` est sondée (`statfs`, `st_dev`, stabilité des inodes, reflink, `openat2`) ; chaque racine physique distincte a **sa** sentinelle `.shelfhold-mount-id`, **son** `.shelfhold/staging`, **sa** corbeille `.shelfhold/recycle` et son contrôle de bail. Une racine absente bloque les écritures vers elle seule (`mount.sentinel_missing`). Une écriture qui changerait de racine passe par copie, vérification et intention, jamais par `rename` (EF-EXP-059, invariant I24).
- **Identité de fichier** : (inode, taille, mtime, puis hash en cas de doute) ; `dev` ne sert qu'à décider « même racine », car il change après un remontage NFS ou sur CIFS sans `serverino`.
- **`adopt_ro`** : entrée de `PathMap` en lecture seule pour des fichiers adoptés hors de `/library` (volume de téléchargement de Suwayomi monté directement dans Kavita, montage `:ro` dédié, par exemple `/upstream/suwayomi`) ; lisible par `safe_open` sous racine, jamais cible d'écriture, source du rapatriement ; le montage est retiré après la bascule (EF-EXP-060).
- Test nightly contre un Kavita réel : un fichier déposé dans `.shelfhold/` n'est jamais indexé ; sinon, le motif est ajouté à `ExcludePatterns` avec confirmation.
- **Chemin figé** : le chemin d'un fichier exporté ne dépend que des attributs figés à sa première exportation (ADR-0040, EF-EXP-061).

## Amendement passe 7 (2026-11-03)
G10 (GAP-7-13, GAP-7-14).
- Le **bail d'instance** est pris **par racine physique** (`<racine>/.shelfhold/lease`) à l'ouverture d'une session d'écriture vers cette racine (ENF-OPS-015 aligné sur EF-EXP-059).
- Avec plusieurs racines physiques, `/store` ne partage pas le système de fichiers de toutes ; la topologie type le documente et `shelfhold status` (page Santé en M3) indique, par racine, si le reflink ou le lien dur vers `/store` est possible, sinon le repli en copie.
- Après une restauration, une sentinelle **en avance** sur la base place l'instance en `restored_behind` (ADR-0044).

## Amendement passe 9 (2026-11-03)
J5 (GAP-9-04, GAP-9-09).
- Le compteur de génération augmente **une fois par session d'écriture** : commit de la base d'abord, réécriture atomique de la sentinelle ensuite. Au démarrage, base = sentinelle : normal ; base en avance **d'un cran** : arrêt entre les deux, normal, sentinelle ré-estampillée ; en avance de plus d'un cran : copie divergente ; en retard : `restored_behind` (ADR-0044). Invariant I39.
- `.shelfhold-mount-id` et `.shelfhold/**` sont des **chemins réservés** : aucun plan `FsOp` de destination ne peut les toucher (I37).

## Amendement passe 10 (2026-11-03)
K9 (GAP-10-05, GAP-10-12).
- Une sentinelle **durablement** différente, absente ou en retard a une sortie : `shelfhold root takeover|reinit|restored` (M1b) et les `waiting_state` `foreign_root`, `root_lost`, `root_behind` ; `takeover` garde la liste des `instance_id` prédécesseurs dans la sentinelle, pour que le miroir reste lisible (passe 11 : les pierres tombales restent déchiffrables par les `kid` du trousseau importé, plus par un sel) ; aucune de ces commandes ne supprime `.shelfhold/`.
- Même logique pour `/store` : `shelfhold store reinit` (`store_lost`) ; une purge amont revérifie la copie de `/store` au moment de l'exécution.

## Amendement passe 11 (2026-11-03)
L1 (GAP-11-02). `root takeover` ne garde plus de sels : les pierres tombales sont chiffrées sous des `kid` du trousseau (ADR-0048) ; une racine reprise d'une instance perdue reste protégée tant que le trousseau de l'instance prédécesseure est importé (`secrets import`), sinon ses lignes sont inertes (`tombstone.unverified`). Test renommé `test_foreign_root_takeover_keeps_predecessor_kids`. La sentinelle porte aussi, dès M1b, le nombre de lignes et l'étiquette de la dernière ligne du journal de pierres tombales (détection de troncature, GAP-11-12).

## Exigences liées
EF-EXP-003, EF-EXP-006, EF-EXP-015, EF-EXP-018, EF-EXP-028, EF-EXP-029, EF-UX-002, EF-BIB-006, ENF-SEC-012, ENF-OPS-006, ENF-OPS-011, EF-EXP-043, EF-EXP-045, ENF-OPS-015, EF-EXP-004, EF-EXP-009, EF-EXP-034, EF-EXP-039, EF-EXP-029, EF-EXP-052, EF-EXP-054, EF-EXP-057, EF-EXP-059, EF-EXP-060, EF-EXP-061, EF-INT-026, EF-EXP-001, EF-EXP-064, EF-BIB-054.

## Sources
- https://man7.org/linux/man-pages/man2/openat2.2.html
- https://man7.org/linux/man-pages/man2/rename.2.html (EXDEV)
- https://wiki.kavitareader.com/guides/scanner/managefiles/
- https://wiki.kavitareader.com/guides/admin-settings/libraries/ (`ExcludePatterns`)
- https://github.com/Kareadita/Kavita/blob/develop/Kavita.Server/Controllers/PluginController.cs (`plugin/parse`)
- https://docs.docker.com/engine/storage/bind-mounts/
- `deliverables/review/contradicteur-ops-secu-legal.md` (OBJ-OPS-01, -02, OBJ-SEC-03), `chasseur-trous-passe1.md` (GAP-1-04, -11), ARBITRAGES A20, A21, A25, A27
- https://wiki.kavitareader.com/guides/scanner/
- `deliverables/review/explorateur-passe3.md` (P1, P4, P8)
- `deliverables/review/chasseur-trous-passe4.md` (GAP-4-09, GAP-4-10)
- `deliverables/review/explorateur-passe4.md` (R5)
- `deliverables/review/ARBITRAGES-passe4.md`
- `deliverables/review/chasseur-trous-passe5.md`
- `deliverables/review/explorateur-passe5.md`
- `deliverables/review/ARBITRAGES-passe5.md`
- `deliverables/review/chasseur-trous-passe7.md`, `deliverables/review/explorateur-passe7.md`, `deliverables/review/ARBITRAGES-passe7.md` (passe 7)
- `deliverables/review/chasseur-trous-passe9.md`, `deliverables/review/explorateur-passe9.md`, `deliverables/review/ARBITRAGES-passe9.md` (passe 9)
- `deliverables/review/chasseur-trous-passe10.md`, `deliverables/review/explorateur-passe10.md`, `deliverables/review/ARBITRAGES-passe10.md` (passe 10)
- `deliverables/review/chasseur-trous-passe11.md`, `deliverables/review/explorateur-passe11.md`, `deliverables/review/ARBITRAGES-passe11.md` (passe 11)

## Amendement passe 12 (2026-11-03)
M2, M9 (GAP-12-02, AA1).

- La sentinelle `.shelfhold-mount-id` porte, à côté de l'identifiant d'instance et du compteur de génération, un **`root_id`** (ULID propre à la racine), distinct de l'`instance_id` et conservé par `root takeover` et `rebaseline` ; il lie les pierres tombales de la racine (ADR-0048).
- Clé Kavita : clé nommée dédiée à expiration, **Kavita ≥ 0.9.1** (minimum unique depuis la passe 12), liste blanche des routes dans `docs/kavita-routes.md`.

Source : `deliverables/review/chasseur-trous-passe12.md`, `deliverables/review/explorateur-passe12.md`, `deliverables/review/ARBITRAGES-passe12.md` (passe 12)

## Amendement passe 13 (2026-11-03)
N5 (GAP-13-04, GAP-13-05).

- Table `physical_root(root_id, entrée PathMap, st_dev vu, génération, prédécesseurs)` dès M1a : le `root_id` d'une racine n'existe plus seulement dans la sentinelle ; `root takeover` et la recréation d'une sentinelle perdue le reprennent.
- La sentinelle et le bail sont des formats versionnés (ADR-0039) ; un retour arrière ne perd ni le registre des `kid` (`tombstone_kids`) ni les compteurs de troncature.
- Tests : `test_takeover_reuses_root_id_from_physical_root`, `test_sentinel_m1b_fields_preserved_by_m1a_rewrite`.

Source : `deliverables/review/chasseur-trous-passe13.md`, `deliverables/review/explorateur-passe13.md`, `deliverables/review/ARBITRAGES-passe13.md` (passe 13)
