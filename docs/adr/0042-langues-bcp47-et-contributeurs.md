---
status: accepté
date: 2026-11-03
amended: 2026-11-03 (passe 7)
decision-makers: mainteneur
---
# ADR-0042 — Langues BCP 47 et contributeurs

## Contexte et problème
La langue fait partie de la clé d'unicité de l'Édition (ADR-0021, I15), mais aucun livrable n'en fixait le format (U2). Les mêmes chapitres arrivent avec des dialectes différents : codes Mihon (`zh-Hans`, `zh-Hant`, `fil`, `pt-BR`, `es-419`), codes d'une extension multilingue de Mihon (`zh-hk`, `tl`, `pt-br`, `es-la`, et des pseudo-codes romanisés `ja-ro`), `lang = "all"` au niveau d'une extension multilingue, `LanguageISO` saisi à la main dans ComicInfo, `dc:language` libre en OPDS. Sans canonisation : deux Éditions « pt-BR » et « pt-br », deux séries Kavita, un appariement adoption ↔ suivi qui échoue (GAP-6-24).
Le contributeur n'est qu'un `name` + `sort_name` (U3) : pas de variantes d'écriture, pas d'ordre des noms, pas d'identifiant externe ; les dates de décès utiles au domaine public sont posées sur l'Édition ; et Kavita coupe chaque champ de personne de ComicInfo à la virgule, si bien que « Oda, Eiichiro » devient deux personnes. Les dates sont aussi mal typées : `date_upload = 0` (inconnu) devient 1970, `2023` devient `2023-01-01` exporté avec une fausse précision (U4). Enfin, Wikidata a été évoqué comme pivot d'identité (U5).

## Facteurs de décision
- Une colonne de clé d'identité doit être normalisée avant la première donnée.
- Aucune personne dupliquée ou coupée dans Kavita dès M1a.
- Calcul du domaine public correct par construction (ADR-0032).
- Ne rien inventer : ni langue, ni précision de date.

## Options considérées
### A. Garder les valeurs brutes (situation passe 5)
- Avantages : aucun travail.
- Inconvénients : Éditions en double, fusions manuelles, personnes coupées dans Kavita.
### B. BCP 47 canonique validé par l'hôte, `contributor` + `contributor_name`, dates partielles typées, Wikidata en fournisseur optionnel
- Avantages : clé stable ; une personne nommable et fusionnable ; dates honnêtes ; identité interne indépendante d'un tiers.
- Inconvénients : une table de correspondance de langues à maintenir (données CLDR) ; deux tables de plus.
### C. Wikidata comme pivot d'identité (QID pour Œuvres et personnes)
- Avantages : identifiants partagés, liens vers AniList, MangaUpdates, Gutenberg, BnF.
- Inconvénients : couverture mesurée faible le 2026-11-03 (environ 9 000 éléments avec un identifiant AniList, 4 000 avec un identifiant Gutenberg) ; quasi nulle sur la longue traîne webtoon et scanlation ; un pivot absent pour la majorité des Œuvres ne peut pas être l'identité.

## Décision
Option B (arbitrages F10, F11, F12).
- **Langues (ENF-I18N-005, invariant I23)** : toute langue stockée est une étiquette **BCP 47 canonique** (RFC 5646 ; langue minuscule, écriture en casse titre, région en majuscules ; alias CLDR : `tl`→`fil`, `iw`→`he`, `es-la`→`es-419`, `ja-ro`→`ja-Latn`). La valeur reçue est gardée (`lang_raw`, avec provenance). Le SDK porte un type `LanguageTag` construit par l'hôte (`normalize_lang()` unique ; les plugins ne normalisent pas). Le chinois exige l'écriture (`zh-Hans`, `zh-Hant`) : jamais `zh` nu dans une clé d'Édition. `all`, `multi`, `und` sont interdits dans la clé d'Édition : la Variante prend la langue du chapitre si la source la donne, sinon `waiting_state = language_unknown` (sortie `shelfhold edition set-lang`). À l'export, `LanguageISO` suit l'attente de Kavita ; pour une série adoptée, la valeur figée à l'adoption prime (EF-EXP-052).
- **Contributeurs (EF-BIB-046)** : `contributor` (`kind` personne ou organisation, `birth_date?`, `death_date?` partielles) et `contributor_name` (`name`, `lang`, `script`, `kind` display/native/romanized/sort, `family_first`) ; `external_id.entity_type` inclut `contributor` (VIAF, ISNI, BnF, Wikidata, AniList, MangaUpdates) ; `merge_suggestion` étendu aux contributeurs. Les dates de vie sont sur la personne ; ADR-0032 calcule sur toutes les contributions (amendée).
- **Invariant d'export I21** : aucun nom de personne exporté dans ComicInfo ne contient de virgule (forme d'affichage dans l'ordre naturel de la langue choisie ; virgule interne remplacée et journalisée) ; plusieurs personnes sont jointes par `, ` ; `sort_name` n'est jamais écrit dans un champ de personne (OPF : `file-as` seulement).
- **Dates (EF-BIB-047)** : `published_on` en texte ISO 8601 partiel + `date_precision` (year, month, day) ; `work.first_published` distinct de la date d'Édition ; `0` → NULL ; jamais de champ ComicInfo ou OPF plus précis que la donnée (EDTF niveau 0).
- **Wikidata** : fournisseur de **correspondances** optionnel (M4) : un QID connu propose des `external_id` liés, toujours en suggestion (EF-BIB-003). L'identité reste l'ULID interne. Clés `provider` stables et documentées ; ISBN-10 converti en ISBN-13.

## Conséquences
- Positives : plus d'Éditions en double par dialecte ; personnes propres dans Kavita ; avertissement de domaine public juste pour les illustrateurs et co-auteurs ; aucune dépendance d'identité envers un tiers.
- Négatives : des Unités en `language_unknown` à traiter par l'admin pour les sources multilingues mal renseignées ; table d'alias à suivre avec CLDR.

## Risque principal
Une règle d'alias qui fusionne deux publics distincts (par exemple `zh-hk` ramené à `zh-Hant` alors qu'une équipe distingue Hong Kong et Taïwan). Mitigation : `lang_raw` conservé, table d'alias versionnée et testée sur un corpus d'étiquettes réelles relevées au spike S5, fusion réversible par `merge_log`.

## Réversibilité
Moyenne : la canonisation se rejoue à partir de `lang_raw` ; les tables de contributeurs sont additives.

## Amendement passe 7 (2026-11-03)
G6, G8 (GAP-7-06, GAP-7-08).
- `normalize_lang()` est un normaliseur **versionné** (`normalizer_id@version` stocké avec chaque étiquette) ; une mise à jour des alias CLDR est une migration explicite avec aperçu des fusions (ENF-OPS-021, ADR-0043).
- `text_version` référence un `contributor` (traducteur publié) ou un libellé contrôlé ; une fusion de contributeurs qui ferait coïncider deux Éditions passe par un plan de fusion d'Éditions (EF-BIB-049).

## Exigences liées
ENF-I18N-005, EF-BIB-046, EF-BIB-047, EF-BIB-001, EF-BIB-003, EF-SRC-024, EF-EXP-052, ENF-QUAL-006, ENF-OPS-021, EF-BIB-049.

## Sources
- `deliverables/review/explorateur-passe6.md` (U2, U3, U4, U5)
- `deliverables/review/chasseur-trous-passe6.md` (GAP-6-24)
- `deliverables/review/ARBITRAGES-passe6.md` (F10, F11, F12)
- https://www.rfc-editor.org/rfc/rfc5646
- https://github.com/unicode-org/cldr
- https://github.com/Kareadita/Kavita/blob/develop/Kavita.Models/Metadata/ComicInfo.cs (découpage des noms à la virgule)
- https://github.com/mihonapp/mihon/blob/main/source-api/src/main/kotlin/eu/kanade/tachiyomi/source/model/SManga.kt
- https://www.loc.gov/standards/datetime/ (EDTF)
- https://www.wikidata.org/wiki/Property:P8731 · https://www.wikidata.org/wiki/Property:P2034 · https://query.wikidata.org/
- `deliverables/review/chasseur-trous-passe7.md`, `deliverables/review/explorateur-passe7.md`, `deliverables/review/ARBITRAGES-passe7.md` (passe 7)
