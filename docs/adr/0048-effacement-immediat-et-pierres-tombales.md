---
status: accepté
date: 2026-11-03
amended: 2026-11-03 (passes 10 et 11) ; 2026-11-03 (passe 12)
decision-makers: mainteneur
---
# ADR-0048 — Effacement immédiat et pierres tombales

## Contexte et problème
Le modèle de Shelfhold est fait pour **ne pas perdre** : corbeille de 30 jours (EF-BIB-021), suppression définitive seulement quand aucune sauvegarde retenue ne référence la copie, « Mettre à l'abri » automatique vers `/store` (EF-BIB-038), I11, miroir de provenance (ADR-0046), relevés amont, « Combler les trous » et restauration (ADR-0044) qui peuvent ramener une Unité.

L'explorateur de la passe 9 (X2) rappelle qu'un foyer peut devoir faire disparaître **tout de suite et partout** un contenu reçu d'une source tierce (contenu dont la détention est illicite, injonction, notification d'un ayant droit, erreur grave). Une exigence en vigueur (EF-BIB-021) l'interdisait : sans décision, l'implémentation maintiendrait la détention pendant la corbeille et la rétention des sauvegardes, et une restauration ferait réapparaître le contenu.

## Facteurs de décision
- Effacer toutes les copies **gérées**, tout de suite, sans exception de rétention.
- Ne jamais réacquérir ni réadopter ce qui a été effacé, y compris après une restauration de `/data`.
- Ne garder aucune trace lisible (titre, identifiant, empreinte vérifiable par dictionnaire) de ce qui a été effacé.
- Rester efficace après une migration d'identité (normaliseur, algorithme d'empreinte, référence de source).
- Dire honnêtement ce qui reste hors de portée.

## Options considérées
### A. Suppression ordinaire puis vidage manuel de la corbeille
- Avantages : rien de nouveau.
- Inconvénients : copies de `/store`, staging, miroir, Suwayomi et sauvegardes oubliées ; réacquisition par le suivi ou « Combler les trous » ; réapparition après restauration.
### B. Purge immédiate avec pierre tombale en base seulement
- Avantages : simple ; bloque la réacquisition.
- Inconvénients : une restauration de `/data` antérieure à la purge efface la pierre tombale, et le contenu revient.
### C. Purge immédiate, pierre tombale en base **et** journal hors base par racine, rejoué après restauration (retenue)
- Avantages : la pierre tombale survit à une restauration de `/data` (le journal vit sur la racine, hors du jeu de sauvegarde) ; blocage complet.
- Inconvénients : un format sur disque de plus ; un journal que l'utilisateur pourrait effacer (la base garde alors la trace tant qu'elle n'est pas restaurée).

## Décision
Option C (arbitrage J2).
- `shelfhold purge --now <œuvre|unité>` : admin, **confirmation tapée**, audité. Supprime toutes les copies gérées (destinations, `/store`, corbeille, staging, miroir, vignettes, `/data/meta`, titres des relevés amont), **retire la revendication Suwayomi**, passe outre corbeille, mise à l'abri, I11 et rétention de sauvegarde pour les copies gérées. Rapport des copies effacées et de celles hors de portée.
- Table **`content_tombstone`** : portée, motif opaque, époque, et les **valeurs brutes** des identifiants (références amont telles que reçues, place, `external_id`, `content_hash`/`derived_hash` avec leur algorithme, versions de normaliseurs en vigueur) **chiffrées en AEAD** sous une clé dérivée du trousseau (passe 11, L1 ; la passe 9 prévoyait une empreinte de référence salée par l'identifiant d'instance et un `content_hash` en clair, remplacés) ; bloque réacquisition, « Combler les trous », suivi, adoption, réadoption et import d'un fichier de même empreinte, par un test d'appartenance sur un index déchiffré en mémoire.
- **Journal hors base** `<racine>/.shelfhold/tombstones` (par racine `rw`, ajout seul, `0600`, chemin réservé, format versionné, **2.0** depuis la passe 11 : lignes chiffrées et authentifiées), rejoué au démarrage et après toute restauration, avant la réadoption (ADR-0044). Invariant I36.
- Ni titre ni identifiant lisible dans la table, le journal ou l'audit : rien n'est lisible sans le trousseau.
- Documentation : les sauvegardes externes de l'hôte, les caches de Kavita et les copies faites hors de Shelfhold restent **hors de portée**, à purger par l'admin.
- Action **réservée** : session fraîche ou CLI locale seulement, jamais une clé d'API ni un jeton d'appareil (passe 11, L2, EF-USR-024).
- Jalons : M1a (CLI, table, journal chiffré, `/library`, staging, corbeille, miroir, Suwayomi), M1b (`/store`, sauvegardes intégrées, `store/INDEX.jsonl`, détection de troncature), M3 (action dans l'UI).

## Conséquences
- Positives : l'utilisateur peut cesser de détenir un contenu sans attendre ; aucune réapparition par le suivi ni par une restauration.
- Négatives : une action irréversible de plus (d'où la confirmation tapée, l'audit et l'exclusion des clés d'API) ; un journal à garder cohérent entre racines ; depuis la passe 11, l'efficacité des pierres tombales dépend du trousseau (perdu sans export ⇒ journal inerte, risque accepté).

## Risque principal
Purge partielle (une copie oubliée, une destination injoignable). Mitigation : calcul par `replica` (source unique des exemplaires, EF-BIB-045), exécution sous intention reprise après un arrêt, rapport des copies restantes, nouvelle tentative au retour de la destination, rejeu du journal à chaque démarrage. Risque second (passe 11) : trousseau perdu sans export, pierres tombales inertes (le contenu purgé redevient acquérable) ; mitigation : export du trousseau rappelé après la première pierre tombale et inclus au kit de secours (EF-USR-023), avertissement dans `shelfhold status`.

## Réversibilité
Le contenu effacé ne revient pas (c'est le but). La pierre tombale peut être levée par l'admin (`shelfhold tombstone lift`, confirmation tapée, audité) ; le mécanisme peut être retiré sans migration. Le format chiffré (passe 11) peut changer d'algorithme par `kid`/`alg` et réécriture en avant, puisque les valeurs brutes restent déchiffrables tant que le trousseau existe ; revenir à des empreintes salées serait possible mais réintroduirait la fuite par dictionnaire.

## Amendement passe 10 (2026-11-03)
K1, K2, K9 (GAP-10-01, GAP-10-02, GAP-10-09, GAP-10-14).
- **Portées** : `content_tombstone.scope ∈ {variant, place, series_binding, work}` dès M1a, dans la table et le journal. Une purge d'Unité pose `variant` et `place` (une Variante re-téléversée sous un nouvel identifiant à la même place est bloquée) ; une purge d'Œuvre pose en plus `series_binding` (clé de série amont stable, jamais un titre ; sinon la référence telle quelle — passe 10 : « empreinte salée », remplacée en passe 11 par la valeur brute chiffrée) et, en M2, `work` (identifiants externes). Le suivi ignore une série amont sous pierre tombale ; en M1a, alerte « retirer la série dans Suwayomi » ; en M1b, `unfollow`. Sans portée `series_binding`, la `series_ref` effacée par la purge n'aurait jamais permis de poser ce blocage après coup.
- **Journal authentifié** : passe 10 : chaque ligne porte `kid`, `salt_id`, `scope` et un HMAC-SHA256 (clé dérivée du trousseau), une ligne non signée, invalide ou de clé inconnue bloquant l'acquisition sans **jamais** rien supprimer — **remplacé en passe 11** par l'AEAD (format 2.0, sans `salt_id`), où une ligne indéchiffrable est **inerte** (ni blocage ni suppression, `tombstone.unverified`) ; une suppression issue d'un rejeu après restauration attend la confirmation de l'admin. Le dossier partagé est écrit par d'autres que Shelfhold ; les droits `0600` ne suffisent pas.
- **Périmètre** : événements, arguments de jobs et journaux ne portent que des ULID ; la purge efface aussi jobs, lettres mortes, notifications en attente et lignes de métadonnées ; le rejeu après restauration efface les lignes remises par l'instantané.
- **Reste documenté** : un retour arrière de tout le pool par l'hôte annule une purge sans que rien ne le détecte ; exporter les pierres tombales hors de l'hôte (`secrets export`).
- Risque principal réévalué : une ligne forgée ou une série restée dans Suwayomi ne doit ni effacer ni ramener un contenu (I40, I41).

## Amendement passe 11 (2026-11-03)
L1, L2 (GAP-11-01, GAP-11-02, GAP-11-03, GAP-11-08, GAP-11-12).

**Problème.** Les empreintes de la passe 10 étaient (a) calculées sur des valeurs **normalisées** : une migration de normaliseur (`normalize migrate`, `content_hash@v2`, `migrate_ref`, alias, `replaced_by`, fusion d'Éditions) recalcule les lignes vivantes mais jamais une pierre tombale, dont la préimage a été effacée — le contenu purgé revient en silence (GAP-11-01) ; (b) **salées par l'identifiant d'instance**, public dans `.shelfhold-mount-id`, sur des préimages de faible entropie (URL avec slug du titre), et `content_hash` était en clair : une attaque par dictionnaire ou par confirmation révèle ce qui a été purgé (GAP-11-02). Le HMAC protégeait l'intégrité, pas la confidentialité.

**Options considérées.**
- A. Empreintes à clé `HMAC(K_fp, domaine ‖ forme canonique)` plus, pour chaque composante, la version du normaliseur qui l'a produite, et calcul de l'empreinte du candidat sous chaque version citée (propositions du chasseur). Confidentiel, mais deux mécanismes à tenir (clé d'empreinte et matrice de versions), et une composante dont la forme canonique change de colonnes (ajout de `packaging`) exige une règle de canonisation figée par version.
- B. **Valeurs brutes chiffrées en AEAD** (retenue, L1) : la pierre tombale garde les identifiants tels que reçus, chiffrés ; le test d'appartenance déchiffre puis **re-normalise** les deux côtés avec le normaliseur courant et chaque version historique encore citée. Une seule clé, un seul mécanisme, insensible aux migrations ; l'intégrité est incluse (étiquette AEAD).
- C. Statu quo (sel public, `content_hash` en clair) : simple, mais confidentialité illusoire et pierres tombales aveugles aux migrations.

**Décision (option B).**
- Table et journal stockent, pour chaque pierre tombale, les **valeurs brutes** (références amont, place avec la clé d'Édition brute et ses colonnes à sentinelle, `external_id`, `content_hash`/`derived_hash` avec leur algorithme) et les `normalizer_id@version` en vigueur, chiffrés en **XChaCha20-Poly1305** (nonce aléatoire de 192 bits) sous une clé **HKDF-SHA256** dérivée du trousseau (contexte « pierres tombales », distincte des clés de secrets), dans l'enveloppe `{v, kid, alg, nonce, ct}` ; **données associées** = version de format ‖ portée ‖ racine (`root_id` depuis la passe 12, M2 — `mount_id` avant —, ou `db` pour la table) ‖ `seq`. Journal au format **2.0** (remplace 1.1 avant toute écriture réelle ; rien n'étant livré, aucune migration). `salt_id` disparaît ; `root takeover` garde l'usage des `kid` des instances prédécesseures par import de leur trousseau.
- **Test d'appartenance** : index déchiffré **en mémoire seulement**, reconstruit au démarrage, après chaque rejeu, chaque purge et chaque `normalize migrate` ; comparaison sous le normaliseur courant et chaque version citée ; un fichier est haché avec chaque algorithme de `content_hash` cité. Une version citée n'est **jamais retirée** du code (ADR-0043, contrôle CI).
- **Migrations d'identité** (`normalize migrate`, fusion d'Éditions, `migrate_ref`, alias, `replaced_by`) : consultent les pierres tombales ; une ligne vivante qui, migrée, correspondrait à une pierre est bloquée (`tombstone.migration_hit`) ; l'alias inverse est passé au test.
- **Ligne indéchiffrable** (clé perdue constatée — depuis la passe 12, une ligne au `kid` inconnu bloque d'abord, voir l'amendement passe 12 —, ligne altérée, recopiée d'une autre racine, forgée) : **inerte** — ni blocage ni suppression, `tombstone.unverified`, citée au rapport. Une suppression issue d'un rejeu après restauration attend toujours la confirmation de l'admin (I41).
- **Risque accepté** : trousseau perdu ⇒ journal inerte (il ne bloque plus rien et ne supprime jamais rien). `secrets export` exporte le trousseau dans un fichier chiffré séparé, que le kit de secours du foyer (EF-USR-023) **référence** sans contenir aucune clé (précisé en passe 12, M8) ; `shelfhold status` avertit tant qu'aucun export n'a suivi la première pierre tombale (Q64, R62).
- **Troncature** (GAP-11-12, M1b) : nombre de lignes et étiquette de la dernière ligne recopiés dans la sentinelle et la base ; un écart émet `tombstone.journal_truncated` et le journal est complété depuis la table ou une autre racine.
- **Périmètre** (GAP-11-08, M1b) : la purge réécrit `store/INDEX.jsonl` dans son intention.
- **Crédential** (L2) : `purge --now`, `tombstone lift` et `tombstone list` sont des actions réservées, refusées à toute clé d'API (portée `admin` comprise) et à tout jeton d'appareil ; session fraîche ou CLI locale seulement (EF-USR-024, ADR-0049).
- Invariant I46 (et I36, I40, I41 réécrits pour l'AEAD).

## Exigences liées
EF-BIB-052, EF-BIB-021, EF-BIB-038, EF-INT-026, EF-INT-015, EF-EXP-063, EF-EXP-001, ENF-LEG-006, EF-BIB-053, EF-SRC-029, EF-INT-016, EF-BIB-054, EF-USR-024, ENF-SEC-005, ENF-OPS-021, EF-USR-023.

## Sources
- `deliverables/review/explorateur-passe9.md` (X2), `deliverables/review/ARBITRAGES-passe9.md` (J2)
- https://www.legifrance.gouv.fr/codes/article_lc/LEGIARTI000043409305 (Code pénal, art. 227-23)
- https://www.rfc-editor.org/rfc/rfc8493 (BagIt : manifestes à côté de la charge utile)
- https://jsonlines.org/
- `deliverables/review/chasseur-trous-passe10.md`, `deliverables/review/explorateur-passe10.md`, `deliverables/review/ARBITRAGES-passe10.md` (passe 10)
- `deliverables/review/chasseur-trous-passe11.md` (GAP-11-01, GAP-11-02, GAP-11-03, GAP-11-08, GAP-11-12), `deliverables/review/ARBITRAGES-passe11.md` (L1, L2) (passe 11)
- https://www.rfc-editor.org/rfc/rfc5869 (HKDF)
- https://www.rfc-editor.org/rfc/rfc8439 (ChaCha20 et Poly1305 pour les protocoles IETF)
- https://datatracker.ietf.org/doc/draft-irtf-cfrg-xchacha/ (XChaCha20-Poly1305)
- https://doc.libsodium.org/secret-key_cryptography/aead/chacha20-poly1305/xchacha20-poly1305_construction (AEAD XChaCha20-Poly1305 de libsodium)

## Amendement passe 12 (2026-11-03)
M1, M2, M7, M8 (GAP-12-01, GAP-12-02, GAP-12-07, GAP-12-08, GAP-12-14). Passe sans manque significatif : règles et contrôles, aucun changement de format.

- **Rotation (M1)** : un `kid` cité par une pierre tombale (table, journal d'une racine, ligne en attente) n'est **jamais retiré** du trousseau, même au-delà de la rétention des sauvegardes (contrôle au démarrage, `secrets rotate --retire` refusé) ; la rotation **ne réécrit jamais** le journal (ajout seul) : les nouvelles lignes prennent le nouveau `kid`, les anciennes gardent le leur. La phrase « rechiffrement sous un nouveau `kid` par rotation » de la grille est retirée. `shelfhold status` avertit après chaque rotation tant qu'aucun nouvel export n'a suivi.
- **`kid` inconnu (M1)** : au rejeu, une ligne dont le `kid` est inconnu du trousseau courant (sauvegarde de `/data` antérieure à une rotation) n'est **pas** inerte : elle bloque `reconcile --adopt-after-restore` et la réacquisition de sa portée (en clair) sur sa racine, émet `tombstone.key_unknown` et l'alerte « réimporter le trousseau ». Sortie : `secrets import` du dernier export, ou dérogation réservée, tapée et auditée `shelfhold tombstone accept-lost-keyring`, après laquelle ces lignes deviennent inertes. Une ligne qui échoue à l'authentification sous un `kid` connu reste inerte. Ainsi I36 tient aussi pour une restauration antérieure à une rotation ; le risque accepté (R62) se limite à la perte constatée du trousseau.
- **`root_id` (M2)** : les données associées portent le `root_id`, ULID propre à la racine écrit dans la sentinelle, distinct de l'`instance_id` et conservé par `root takeover` et `rebaseline` ; une racine reprise garde ses lignes déchiffrables.
- **Suwayomi en M1a (M7)** : la purge n'écrit pas dans Suwayomi en M1a : retrait de la revendication côté Shelfhold, copie amont listée au rapport, alerte « supprimer dans Suwayomi » ; suppression du téléchargement par `release_claim` à partir de M1b.
- **Kit de secours (M8)** : le kit imprimable ne contient **aucune clé** ; il référence l'export chiffré du trousseau (fichier séparé, phrase de passe) et dit où le ranger. La phrase « le kit de secours et `secrets export` incluent l'export du trousseau » de l'amendement passe 11 se lit désormais ainsi.
- **Racine absente (GAP-12-14)** : en M1a, les lignes destinées à une racine `rw` démontée sont gardées dans `tombstone_pending_root` et écrites au remontage, avant toute autre écriture sur la racine.
- Tests : `test_cited_tombstone_kid_never_retired`, `test_rotation_never_rewrites_tombstone_journal`, `test_restore_pre_rotation_backup_keeps_post_rotation_tombstones`, `test_unknown_kid_line_blocks_reconcile_until_keyring_import`, `test_root_id_distinct_from_instance_id_kept_by_takeover`, `test_purge_m1a_releases_claim_without_writing_suwayomi`, `test_purge_with_absent_rw_root_writes_journal_on_remount`.

Source : `deliverables/review/chasseur-trous-passe12.md`, `deliverables/review/explorateur-passe12.md`, `deliverables/review/ARBITRAGES-passe12.md` (passe 12)

## Amendement passe 13 (2026-11-03)
N1, N5 (GAP-13-01, GAP-13-04, GAP-13-05, GAP-13-06, GAP-13-09, GAP-13-10). Deuxième passe sans manque significatif : précisions additives, aucun format cassé.

- **Registre des `kid` émis (N1)** : table `tombstone_kid` en base et champ `tombstone_kids` dans la sentinelle de chaque racine `rw`, en ajout seul. Au rejeu, une ligne dont le `kid` est absent du trousseau **bloque** la réadoption et la réacquisition de sa portée (`tombstone.key_unknown`, règle de la passe 12) si ce `kid` figure au registre de la base ou de la sentinelle ; sinon elle est **forgée** : inerte, `tombstone.unverified` (cause `kid_unregistered`), alerte. Le `kid` étant en clair et choisi par l'écrivain, la règle de la passe 12 laissait un faussaire suspendre une portée avec un `kid` inventé ; il lui faut désormais écrire aussi la sentinelle (même uid, donc déjà capable de supprimer la bibliothèque : déni de service seulement). Les textes « clé inconnue ⇒ inerte » d'EF-BIB-053 (3), du test d'EF-BIB-054 (5), de la matrice de pannes et de R58 sont réécrits en conséquence.
- **Contrôle « jamais retiré » (GAP-13-10)** : le registre en est la seule source, ce qui couvre les racines démontées ; la rotation ne rechiffre **jamais** la table `content_tombstone` (ajout seul, comme le journal).
- **`physical_root` (GAP-13-04)** : le `root_id` est aussi gardé en base ; `root takeover` et la recréation d'une sentinelle perdue le reprennent ; au rejeu, les `root_id` connus sont essayés et `tombstone.root_mismatch` est émis au lieu d'un journal inerte en silence.
- **Formats (GAP-13-05)** : sentinelle, bail et en-tête du journal sont versionnés (ADR-0039), champs inconnus conservés.
- **`tombstone rekey` (GAP-13-06, M1b)** : après une fuite de clé, action réservée sous intention qui réécrit le journal de chaque racine sous un nouveau `kid` (nouveau fichier puis `rename` atomique, racine démontée réécrite au remontage), réécrit la table et `tombstone_pending_root` et marque l'ancien `kid` compromis. Seule exception à « le journal n'est jamais réécrit » : explicite, réservée, hors de la rotation ordinaire.
- **Purges de M1a (GAP-13-09)** : tickets de téléchargement fermés côté Shelfhold en M1a, `cancel(ticket)` en M1b ; la migration de M1b propose `release_claim` ou `unfollow` pour les copies amont laissées par les purges de M1a, après confirmation.
- Tests : `test_unregistered_kid_line_inert_registered_unknown_kid_blocks`, `test_kid_cited_only_by_unmounted_root_never_retired`, `test_rotation_never_reencrypts_tombstone_table`, `test_replay_tries_known_root_ids_and_reports_root_mismatch`, `test_takeover_reuses_root_id_from_physical_root`, `test_tombstone_rekey_after_leak_rewrites_journal_atomically`, `test_purge_m1a_closes_download_tickets_shelfhold_side`, `test_m1b_migration_offers_release_of_m1a_purged_upstream_copies`.

Source : `deliverables/review/chasseur-trous-passe13.md`, `deliverables/review/explorateur-passe13.md`, `deliverables/review/ARBITRAGES-passe13.md` (passe 13)
