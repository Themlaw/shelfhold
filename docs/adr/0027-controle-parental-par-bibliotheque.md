---
status: accepté
date: 2026-11-02
amended: 2026-11-03 (passe 5) ; 2026-11-03 (passe 7) ; 2026-11-03 (passe 10) ; 2026-11-03 (passe 11) ; 2026-11-03 (passe 12)
decision-makers: mainteneur
---
# ADR-0027 — Contrôle parental par bibliothèque (classer = approuver)

## Contexte et problème
L'objectif O5 promet qu'un enfant ne voit jamais un contenu non classé ou au-dessus de sa limite. Après la passe 1, la chaîne reposait sur l'`AgeRating` ComicInfo exporté vers Kavita (EF-EXP-011). La passe 2 (chasseur de trous, GAP-2-01 à GAP-2-05) a montré quatre trous indépendants :
- un enfant ne peut rien demander, car les résultats distants n'ont presque jamais d'âge et le non-classé était invisible partout (GAP-2-01) ;
- les Œuvres non classées partaient vers Kavita avec `AgeRating=Unknown`, alors que la restriction Kavita a une option « inclure les inconnus » et que l'écran « À classer » n'arrivait qu'en M3 (GAP-2-02) ;
- l'échelle Shelfhold n'avait pas de correspondance définie avec l'énumération ComicInfo (GAP-2-03) ;
- les EPUB ne portaient aucun âge, l'OPF n'étant retouché que pour la série (GAP-2-04) ;
- rien ne vérifiait la configuration des comptes Kavita (GAP-2-05).

Une barrière fondée sur une **métadonnée** échoue dès que la métadonnée manque, est mal lue ou est ignorée par un client (OPDS, liseuse).

## Facteurs de décision
- Une seule erreur de configuration ne doit pas exposer de contenu adulte.
- Fonctionner pour CBZ **et** EPUB, dans Kavita comme dans Shelfhold.
- Laisser un enfant **demander** sans pouvoir **voir** un contenu non classé.
- Rester simple pour l'admin (classer au moment où il approuve).

## Options considérées
### A. Barrière par métadonnée (`AgeRating` ComicInfo + âge OPF), restriction d'âge des comptes Kavita
- Avantages : une seule bibliothèque Kavita par type ; aucune configuration de bibliothèques.
- Inconvénients : dépend de la lecture de l'âge par Kavita pour chaque format ; l'option « inclure les inconnus » ou un EPUB sans âge suffit à exposer ; correspondance d'échelle imparfaite (16+ n'existe pas dans ComicInfo).
### B. Barrière par bibliothèque : étagères à classement maximal liées à des bibliothèques Kavita dédiées, comptes enfants limités à ces bibliothèques ; métadonnée en complément
- Avantages : indépendant du format et de la lecture des métadonnées ; mécanisme d'accès natif de Kavita (droits par bibliothèque), aussi respecté par son OPDS ; l'erreur « Œuvre non classée » est bloquée au plan.
- Inconvénients : une bibliothèque Kavita de plus par type (« Manga famille ») ; une Œuvre qui change d'âge doit changer d'étagère (déplacement).
### C. Pas d'export vers Kavita pour les comptes enfants : lecture seulement dans Shelfhold (M5)
- Avantages : un seul contrôle, celui de Shelfhold.
- Inconvénients : contredit ADR-0020 (Kavita lieu de lecture) ; rien avant M5.

## Décision
Option B (arbitrages B1–B5).
- **Étagère** : `max_age_rating` et `allowed_profiles[]`. Les étagères dont le maximum est < 18+ visent des **bibliothèques Kavita dédiées** dont l'accès n'est donné qu'aux comptes Kavita des enfants (EF-USR-015).
- **Classer = approuver** (B2) : une Œuvre n'entre dans une étagère < 18+ que si elle est classée et sous le maximum ; non classée → étagères adultes seulement ; sinon le plan est **bloqué** (garde de l'étape 0 du pipeline). L'admin classe en approuvant une demande ou en ajoutant. Écran minimal « À classer » (liste + sélecteur) **dès M1** ; version complète en M3.
- **Demandes d'enfant** (B3, EF-USR-014) : recherche limitée aux sources marquées « famille » ; résultat non classé affiché réduit (titre seul, sans couverture) avec la seule action « Demander », jamais lisible ; âge obligatoire à l'approbation (EF-USR-010).
- **Correspondance figée** (B4, EF-EXP-011) : Tous publics → `Everyone` ; 10+ → `Everyone 10+` ; 12+ → `Teen` (passe 11) ; 13+ → `Teen` ; 16+ → `Mature 17+` (plus proche supérieur, documenté) ; 18+ → `Adults Only 18+` ; X → `X18+` ; règle inverse à l'adoption.
- **EPUB** (B5, EF-EXP-033) : écriture OPF autorisée pour `calibre:series`, `series_index` et `belongs-to-collection`, sans réécrire le contenu ; **aucun âge** n'est écrit (ni `shelfhold:age` ni `dc:subject`, retirés en passe 11, L5).
- **Audit** (EF-EXP-036, M3) : lecture des comptes Kavita, alerte en cas d'accès d'un compte enfant à une bibliothèque non famille, de restriction absente ou d'inconnus inclus.

## Conséquences
- Positives : la protection ne dépend plus d'une métadonnée ; l'erreur la plus probable (oubli de classement) est bloquée par construction ; les enfants peuvent demander.
- Négatives : bibliothèques Kavita supplémentaires à créer (proposées par l'assistant, EF-UX-007) ; un changement d'âge qui franchit le maximum provoque un déplacement de fichiers (plan de renommages, avertissement de progression avant M4).

## Risque principal
Compte Kavita d'un enfant configuré à la main avec un accès à une bibliothèque adulte. Mitigations : audit Kavita en lecture (alerte), checklist M3 « OPDS Kavita avec un compte restreint », doc famille.

## Réversibilité
Moyenne : les champs d'étagère et la garde du planificateur sont locaux ; revenir à l'option A ne demande que de lever la garde, mais les bibliothèques Kavita créées resteraient.

## Amendement passe 3 (2026-11-02)
- **Autorité de classement** (C7, EF-BIB-036) : seuls les comptes « classer » (admins) fixent `age_rating` ; âges de fichiers (ComicInfo, OPF), de sous-sources et de fournisseurs = **suggestions** avec provenance, jamais suffisantes pour une étagère < 18+. La règle inverse d'EF-EXP-011 produit une suggestion.
- **Âge effectif = maximum des Éditions** (C8, EF-BIB-037) ; une fusion qui élève l'âge retire l'Œuvre des étagères incompatibles (plan affiché) et n'est jamais automatique si elle vient d'un identifiant externe. Une série Kavita est rangée selon l'âge maximal de ses Œuvres (GAP-3-24) ; un reclassement sans changement d'étagère est poussé par l'API de série Kavita (GAP-3-21).
- **Adoption d'une bibliothèque enfant** (C9, EF-EXP-048) : Œuvres non classées dans « À classer », suivi suspendu, blocage par Œuvre, « classer tout en lot » avec confirmation ; option « retenir tant que non classé » (EF-EXP-050).
- **Audit dès M3** (C10, EF-USR-017) : correspondance déclarative profil ↔ compte Kavita ; une bibliothèque Kavita par palier d'âge utilisé.
- **Destinations cloisonnées** (C11, EF-EXP-047) : capacité `access_partitioning` ; étagère < 18+ ⇒ destination `library` ou `user`, sinon refus.
- **Recherche famille** (GAP-3-28, EF-USR-018) : filtre de sujets et mode « masquer les non classés » ; vignettes `Cache-Control: private` (GAP-3-13).

## Amendement passe 4 (2026-11-03)
(D1, D13, GAP-4-01, GAP-4-12) : le contrôle parental est livré en **M1b** ; en M1a, seule une étagère adulte (18+ ou X) existe et l'adoption d'une bibliothèque marquée « enfants » est refusée. L'écran « À classer » exige le socle d'authentification (ADR-0018). **Âge par Édition** : une Édition dont l'âge est fixé explicitement par un admin est admise sur cet âge (adaptation jeunesse) ; sinon l'âge effectif de l'Œuvre s'applique ; la série Kavita devient alors `Œuvre + Édition`, et une série Kavita n'appartient qu'à une étagère par bibliothèque distante.

## Amendement passe 5 (2026-11-03)
E6 (GAP-5-06). Dès M1a, **audit Kavita en lecture seule** avant toute liaison d'étagère ou adoption (EF-EXP-055) : une bibliothèque à laquelle accède un compte restreint, sans « inclure les inconnus », ou associé à un profil enfant est **exposée aux enfants** ; une étagère au-dessus du palier de ce compte y est refusée (en M1a : toute étagère). Audit rejoué chaque jour, écart → `target.child_exposure`. **Risque résiduel** (accès modifié dans Kavita entre deux audits, compte enfant non marqué) : attribué au **propriétaire, rôle admin de Kavita**, listé dans `06`.

## Amendement passe 7 (2026-11-03)
G10 (GAP-7-12).
- Un support qui couvre plusieurs Œuvres (recueil, anthologie ; usage réservé) prend pour âge le **maximum** des Éditions couvertes ; son admission en étagère se fait sur cet âge (EF-BIB-037, invariant I6 précisé).

## Amendement passe 9 (2026-11-03)
J3 (U1).
- Les avis de sécurité de Kavita du 2026-09-05 (versions ≤ 0.9.0.2, corrigées en 0.9.1) décrivent un téléchargement de pages et une lecture d'annotations **sans contrôle d'accès par bibliothèque ni restriction d'âge** : la barrière principale de cette ADR est directement touchée.
- `known_bad_versions` (ENF-COMP-001) porte une **catégorie** ; Kavita ≤ 0.9.0.2 y figure en catégorie `access_control`. Sur une telle version, toute étagère dont le palier est < 18+ est **bloquée** (`target.child_exposure`, EF-EXP-055) au lieu d'un avertissement ; **minimum 0.9.1** pour les étagères famille, étendu à toutes les étagères en passe 12 (amendement passe 12).
- L'audit d'exposition s'agrège par **bibliothèque du serveur** (`remote_library`, GAP-9-11), pas par liaison d'étagère.

## Amendement passe 10 (2026-11-03)
K4, K5 (GAP-10-04, Y1) ; **amende l'option retenue par J3**.
- Sur une version `access_control`, tout compte non administrateur atteint toutes les bibliothèques de l'instance ; la règle de base (pas d'étagère 18+ sur une bibliothèque exposée aux enfants) vaut donc pour toute l'instance. Si l'audit trouve **au moins un compte restreint** (âge, « inclure les inconnus » coupé, bibliothèques limitées, ou profil enfant), **toutes** les étagères liées à l'instance, 18+ et X compris, sont mises en pause avec `target.child_exposure`, dès M1a ; sans compte restreint, avertissement seulement. Invariant I43.
- Classer = approuver : classer exige une **authentification fraîche** (`fresh_auth_s`, M1b, ADR-0049), pour qu'une session admin laissée ouverte sur un appareil partagé ne serve pas à franchir la barrière ; l'audit enregistre `session_id` et `auth_time`.
- Hors de portée, documenté : une session adulte de Kavita ouverte sur un appareil partagé ; un compte Kavita par lecteur est recommandé.

## Amendement passe 11 (2026-11-03)
L4, L5 (GAP-11-05, Z3).
- **Édition non classée** (L4, M2, EF-BIB-055) : une Édition créée ou rattachée après le dernier classement de l'Œuvre n'entre **jamais** dans une étagère < 18+ — ni par rattachement automatique par identifiant externe, ni par « Ajouter directement » d'un membre adulte sans droit « classer » — tant qu'un admin ne l'a pas classée ; elle part dans « À classer ». Une Édition existante dont un **âge suggéré** dépasse le classement est bloquée et retirée par plan. Ligne de table de vérité « membre adulte → Ajouter directement » ajoutée. Invariant I48.
- **Âge stocké** (L5) : âge minimal entier (`0`, `10`, `12`, `13`, `16`, `18`) plus drapeau `age_x`, libellés traduits par le catalogue i18n ; comparaisons arithmétiques ; palier **12** ajouté à la correspondance figée : 12+ → `Teen` (plus proche supérieur), règle inverse `12+`/`PEGI 12` → 12+ en suggestion (EF-EXP-011).
- **EPUB** (L5, Z3-a) : la décision B5 d'écrire `shelfhold:age` et un `dc:subject` d'âge est **retirée** : Kavita transforme tout `dc:subject` en genre et ne lit aucun âge dans un EPUB, `shelfhold:age` est une trace de l'outil hors du foyer (contraire à EF-EXP-063) et exige un préfixe déclaré. Aucun âge n'est écrit dans un EPUB exporté ; ces éléments sont seulement **lus** à l'adoption, comme suggestions (`test_epub_export_writes_no_age_subject_nor_tool_meta`). La barrière reste la bibliothèque. Invariant I49 ; Q66, R65.

## Exigences liées
EF-USR-006, EF-USR-010, EF-USR-014, EF-USR-015, EF-EXP-011, EF-EXP-033, EF-EXP-036, EF-UX-007, ENF-SEC-010, EF-BIB-036, EF-BIB-037, EF-EXP-047, EF-EXP-048, EF-EXP-050, EF-USR-017, EF-USR-018, EF-EXP-002, EF-EXP-028, EF-USR-021, EF-EXP-055, ENF-COMP-001, EF-EXP-062, EF-USR-022, EF-BIB-055.

## Sources
- https://github.com/anansi-project/comicinfo (énumération `AgeRating`, brouillon `drafts/v2.1`)
- https://github.com/Kareadita/Kavita/issues/2405
- https://wiki.kavitareader.com/guides/scanner/ (bibliothèques, accès par utilisateur)
- `deliverables/review/chasseur-trous-passe2.md` (GAP-2-01 à GAP-2-05, GAP-2-24), `deliverables/review/ARBITRAGES-passe2.md` (B1–B5)
- `deliverables/review/chasseur-trous-passe3.md` (GAP-3-07 à GAP-3-11), `deliverables/review/ARBITRAGES-passe3.md` (C7–C11)
- `deliverables/review/chasseur-trous-passe4.md` (GAP-4-01, GAP-4-12)
- `deliverables/review/ARBITRAGES-passe4.md`
- `deliverables/review/chasseur-trous-passe5.md`
- `deliverables/review/explorateur-passe5.md`
- `deliverables/review/ARBITRAGES-passe5.md`
- `deliverables/review/chasseur-trous-passe7.md`, `deliverables/review/explorateur-passe7.md`, `deliverables/review/ARBITRAGES-passe7.md` (passe 7)
- `deliverables/review/chasseur-trous-passe9.md`, `deliverables/review/explorateur-passe9.md`, `deliverables/review/ARBITRAGES-passe9.md` (passe 9)
- https://github.com/Kareadita/Kavita/security/advisories/GHSA-x67v-89v8-mf3v
- https://github.com/Kareadita/Kavita/security/advisories/GHSA-gjx7-m655-3grw
- `deliverables/review/chasseur-trous-passe10.md`, `deliverables/review/explorateur-passe10.md`, `deliverables/review/ARBITRAGES-passe10.md` (passe 10)
- https://github.com/Kareadita/Kavita/security/advisories/GHSA-m2v3-fcjh-hm22 (CVE-2026-47202)
- `deliverables/review/chasseur-trous-passe11.md`, `deliverables/review/explorateur-passe11.md`, `deliverables/review/ARBITRAGES-passe11.md` (passe 11)

## Amendement passe 12 (2026-11-03)
M6, M9 (GAP-12-06, GAP-12-09, AA1).

- **Âge de série EPUB (M6)** : aucun âge n'est écrit dans un EPUB (passe 11) ; Kavita classerait donc ces séries « inconnues » et les masquerait aux comptes enfants restreints sans « inclure les inconnus » (que l'audit EF-EXP-036 exige). Dès que l'Œuvre est classée, à la première exportation et à chaque reclassement, l'`AgeRating` de série est **poussé verrouillé par l'API Kavita** pour toute série dont les fichiers ne portent pas d'âge (M2, avant le premier export EPUB) ; le verrou est vérifié après scan. La barrière reste la bibliothèque ; toujours rien dans les fichiers.
- **Édition classée (GAP-12-09)** : `work.rated_at` et `edition.rated_at` sont dans le schéma dès M1b ; une Édition est classée si `edition.rated_at` est non nul ; la règle d'I48 s'applique dès M1b aux étagères famille, à `relink` et à l'appariement adoption ↔ suivi, puis en M2 au rattachement automatique.
- **Version minimale (M9)** : minimum Kavita **unique 0.9.1** pour toutes les étagères (la phrase « 0.8.9 restant le minimum des étagères adultes » de l'amendement passe 9 est remplacée).

Source : `deliverables/review/chasseur-trous-passe12.md`, `deliverables/review/explorateur-passe12.md`, `deliverables/review/ARBITRAGES-passe12.md` (passe 12)

## Amendement passe 13 (2026-11-03)
N3 (GAP-13-03).

- L'âge de série poussé verrouillé (passe 12, M6) est inscrit au registre `field_owner` : c'est le seul cas où Shelfhold réécrit un champ verrouillé dans Kavita ; il est repoussé après une réinstallation de Kavita (`target reconnect`). La barrière reste la bibliothèque dédiée ; l'âge poussé n'est qu'un complément.
- Test : `test_kavita_reconnect_repushes_rated_series_ages`.

Source : `deliverables/review/chasseur-trous-passe13.md`, `deliverables/review/explorateur-passe13.md`, `deliverables/review/ARBITRAGES-passe13.md` (passe 13)
