---
status: accepté
date: 2026-11-03
amended: 2026-11-03 (passe 6) ; 2026-11-03 (passe 7) ; 2026-11-03 (passe 8) ; 2026-11-03 (passe 10) ; 2026-11-03 (passe 11)
decision-makers: mainteneur
---
# ADR-0039 — Formats sur disque versionnés et migrations en avant

## Contexte et problème
Shelfhold écrit dès M1a des fichiers lisibles hors de la base : `unit.json`, `work.json`, manifestes, `config.toml`, sentinelle et fichiers de `.shelfhold/`. ENF-OPS-013 imposait un `schema_version` et une migration « paresseuse, à la lecture ou par job ». Le chasseur de trous de la passe 5 (GAP-5-07, GAP-5-16) relève deux défauts :
- Après un **retour arrière** (tag précédent + sauvegarde pré-migration de la **base**, ENF-OPS-008), une version N peut trouver des fichiers déjà migrés en N+1, hors de toute sauvegarde : elle échoue, ou les réécrit à tort (perte de métadonnées).
- `shelfhold migrate` peut tourner alors que des intentions (`fs_intent`) de la version N sont encore ouvertes, que N+1 rejouerait.
L'emplacement de `unit.json` en mode passage n'était pas dit non plus.

## Facteurs de décision
- Aucun retour arrière ne doit perdre de données ni bloquer le démarrage.
- Pas de code de migration descendante à maintenir (un seul mainteneur).
- Les fichiers doivent rester lisibles par un humain et par un outil tiers (export ouvert, ADR-0023).

## Options considérées
### A. Migration paresseuse libre à la lecture (situation passe 4)
- Avantages : simple, aucune fenêtre d'indisponibilité.
- Inconvénients : un retour arrière trouve des fichiers d'un format plus récent ; comportement indéfini.
### B. `majeur.mineur`, mineur additif lisible par N-1, majeur par job explicite après la fenêtre de retour arrière avec sauvegarde des fichiers touchés, migrations en avant seulement, lecture seule d'un format plus récent, garde de migration sur les intentions
- Avantages : un retour arrière d'une version mineure est toujours sûr ; une migration majeure est réversible par ses sauvegardes ; aucune migration descendante.
- Inconvénients : une migration majeure attend une version mineure de plus ; place disque pour les fichiers sauvegardés.
### C. Migrations descendantes pour chaque format
- Avantages : retour arrière direct.
- Inconvénients : double code à tester pour chaque changement ; contraire à la sobriété de maintenance.

## Décision
Option B (arbitrage E7, GAP-5-07, GAP-5-16).
- `schema_version = "majeur.mineur"` dans chaque format sur disque.
- **Mineur** : ajout de champs seulement ; N-1 lit le fichier et **conserve les champs inconnus** quand il le réécrit.
- **Majeur** : job explicite `formats.migrate`, inscrit comme intention, jamais à la lecture, lancé **après** la fenêtre de retour arrière (au plus tôt à la version mineure suivante), précédé d'une copie des fichiers touchés dans `/data/backups/formats/<révision>/`.
- **En avant uniquement** ; une version qui lit un format plus récent que le sien le traite en **lecture seule** (aucune réécriture, `reconcile.drift`) et refuse toute écriture qui le toucherait.
- **Garde de migration** (ENF-OPS-019) : `shelfhold migrate` refuse s'il reste des intentions ouvertes ; le schéma de `fs_intent` est gelé et versionné, une intention d'un format inconnu n'est jamais rejouée.
- **Emplacement** : `unit.json` vit toujours dans `/data` (`/data/meta/…`), dans tous les modes, jamais dans `/library` ni `/store` (EF-BIB-006).
- **Test** : l'E2E de retour arrière (ENF-OPS-008) couvre un retour après migration des formats, sans perte.

## Conséquences
- Positives : retour arrière sûr pour toute version mineure ; plus de réécriture à tort ; règle simple à vérifier en CI.
- Négatives : changements majeurs plus lents à déployer ; espace disque temporaire pour les sauvegardes de format.

## Risque principal
Un changement présenté comme mineur qui n'est pas additif. Mitigation : test de compatibilité qui fait lire les fichiers de la version N par la version N-1 (fixtures versionnées) et vérifie que les champs inconnus sont conservés.

## Réversibilité
Haute : règle de versionnage ; elle ne fige que l'ordre des opérations.

## Amendement passe 6 (2026-11-03)
F8 (GAP-6-08, GAP-6-25).
- **La base fait autorité.** `unit.json` et `work.json` sont des **projections régénérables** : écrites après la validation de la transaction, vérifiées et réparées au démarrage, reconstruites par `shelfhold meta rebuild`. En cas de divergence, la base gagne (ENF-OPS-020).
- **Arborescence plate et stable** par ULID : `/data/meta/units/<ulid[0:2]>/<ulid>.json` et `/data/meta/works/<ulid[0:2]>/<ulid>.json`, indépendante de la hiérarchie ; une fusion ou une séparation d'Œuvres ne déplace aucun fichier. Elle remplace l'arborescence `/data/meta/works/…/units/` de la passe 5 (format majeur, aucune donnée réelle n'existe encore).
- `meta/` entre dans le jeu de sauvegarde de `/data` (EF-INT-015).
- **Hash** noté avec son algorithme : `sha256:<hex>` dans `unit.json`, les manifestes et les exports.

## Amendement passe 7 (2026-11-03)
G8 (GAP-7-08).
- **Normaliseurs versionnés** (ENF-OPS-021, ADR-0043) : chaque clé d'identité dérivée porte `normalizer_id@version` et sa valeur brute ; un changement de normaliseur est une **migration en avant explicite** (`shelfhold normalize migrate`), avec intention et aperçu des fusions, jamais un recalcul à la lecture ; une version qui rencontre un normaliseur inconnu (plus récent) traite la ligne en lecture seule, comme un format plus récent.

## Amendement passe 8 (2026-11-03)
H1 (GAP-8-01).
- Le **miroir de provenance** `<racine>/.shelfhold/manifest/` (ADR-0046) est un format sur disque versionné `majeur.mineur` : mêmes règles que `unit.json` (champs inconnus conservés par N-1, migration majeure par job explicite, lecture seule si plus récent).
- Il est une **projection** de la base, comme `unit.json` (la base fait autorité) : `shelfhold meta rebuild` le régénère ; il se distingue de `unit.json` par son emplacement (sur chaque racine physique de `/library`, il voyage avec les fichiers) et par son contenu (aucune URL, aucun secret).

## Amendement passe 9 (2026-11-03)
J2, J5.
- Deux formats sur disque de plus suivent `majeur.mineur` : le miroir de provenance passe en **1.1** (lignée par entrée de couverture) et le **journal de pierres tombales** `<racine>/.shelfhold/tombstones` naît en 1.0 (JSON Lines en ajout seul, ADR-0048).

## Amendement passe 10 (2026-11-03)
K1, K2, K7, K9.
- Journal de pierres tombales **1.1** (remplacé par le format 2.0 chiffré en passe 11) : `scope`, `kid`, `salt_id`, `mac` ; migration en avant de 1.0 (lignes de la passe 9 re-signées depuis la table, lignes inconnues de la table laissées non vérifiées : blocage seul).
- Miroir de provenance **1.2** : champs facultatifs `follow_suppressed`, `follow_from`, âge classé (relu en suggestion) ; lecteur 1.1 compatible (champs inconnus conservés).
- `schema.sql` et dictionnaire de données générés et publiés à chaque release ; `store/INDEX.jsonl` versionné (`schema_version`).

## Amendement passe 11 (2026-11-03)
L1 (GAP-11-01, GAP-11-02, GAP-11-12). Journal de pierres tombales **2.0** : ligne `{v:"2.0", seq, scope, kid, alg:"xchacha20poly1305", nonce, ct}`, données associées = version ‖ portée ‖ racine ‖ `seq` ; il **remplace le format 1.1** (HMAC, `salt_id`, `mac`) **avant toute écriture réelle** : rien n'étant livré, aucune migration 1.1 → 2.0 n'est prévue. Toute évolution ultérieure suit la règle des migrations en avant (changement d'`alg` par `kid`, réécriture depuis les valeurs déchiffrées). La sentinelle porte le compteur et la dernière étiquette du journal (M1b).

## Exigences liées
ENF-OPS-013, ENF-OPS-019, ENF-OPS-008, EF-BIB-006, EF-EXP-041, ENF-OPS-020, EF-INT-015, ENF-OPS-021, EF-EXP-063, EF-BIB-052, EF-BIB-053, EF-INT-027, EF-BIB-054.

## Sources
- `deliverables/review/chasseur-trous-passe5.md` (GAP-5-07, GAP-5-16)
- `deliverables/review/ARBITRAGES-passe5.md` (E7)
- https://www.sqlite.org/lang_vacuum.html#vacuuminto
- https://alembic.sqlalchemy.org/en/latest/
- `deliverables/review/chasseur-trous-passe7.md`, `deliverables/review/explorateur-passe7.md`, `deliverables/review/ARBITRAGES-passe7.md` (passe 7)
- `deliverables/review/chasseur-trous-passe8.md`, `deliverables/review/explorateur-passe8.md`, `deliverables/review/ARBITRAGES-passe8.md` (passe 8)
- `deliverables/review/chasseur-trous-passe9.md`, `deliverables/review/explorateur-passe9.md`, `deliverables/review/ARBITRAGES-passe9.md` (passe 9)
- `deliverables/review/chasseur-trous-passe10.md`, `deliverables/review/explorateur-passe10.md`, `deliverables/review/ARBITRAGES-passe10.md` (passe 10)
- `deliverables/review/chasseur-trous-passe11.md`, `deliverables/review/explorateur-passe11.md`, `deliverables/review/ARBITRAGES-passe11.md` (passe 11)

## Amendement passe 13 (2026-11-03)
N5 (GAP-13-05).

- La liste des formats versionnés (ENF-OPS-013) comprend la sentinelle `.shelfhold-mount-id`, le bail `lease` et l'en-tête du journal de pierres tombales : `schema_version` `majeur.mineur`, champs inconnus conservés à la réécriture ; l'E2E de retour arrière (ENF-OPS-008) relit une sentinelle écrite par M1b avec M1a.
- Test : `test_sentinel_m1b_fields_preserved_by_m1a_rewrite`.

Source : `deliverables/review/chasseur-trous-passe13.md`, `deliverables/review/explorateur-passe13.md`, `deliverables/review/ARBITRAGES-passe13.md` (passe 13)
