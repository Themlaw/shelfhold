---
status: accepté
date: 2026-11-03
amended: 2026-11-03 (passe 10) ; 2026-11-03 (passe 12)
decision-makers: mainteneur
---
# ADR-0045 — Progression par fichier

## Contexte et problème
La passe 6 a séparé l'Unité logique (chapitre) du support (`export_item` : tome, EPUB de plage, intégrale). Le côté lecture n'avait pas suivi (V1) : `reading_progress` et `document_alias` étaient clés par `unit_id`, alors que Kavita enregistre la progression par Chapter (un fichier, ou un tome entier sans marqueur de chapitre), que kosync identifie un **fichier** par son hash et qu'un Locator Readium est relatif à une publication. Une page 143 d'un tome de 8 chapitres ne se projette sur aucun chapitre sans connaître les bornes de chaque Unité dans le fichier, et une position n'est vraie que pour un contenu précis. Chez Shelfhold, les supports multi-Unités sont la norme (romans en tomes clos, bibliothèques adoptées en tomes, tomes assemblés en M4).

## Facteurs de décision
- O4 « progression cohérente » est une attente centrale du pont.
- Ne pas perdre l'ancre d'origine : une position fusionnée par Unité ne se reprojette pas.
- `unit_coverage` est remplie dès M1a : ses bornes doivent exister avant.
- Coût faible maintenant (tables vides jusqu'en M4).

## Options considérées
### A. Garder `reading_progress(unit_id)`
- Avantages : aucun travail.
- Inconvénients : états « lu » faux sur toutes les séries en tomes ; 409 « hash ambigu » sur le cas normal ; perte d'information après M4.
### B. Ancre par fichier, progression par Unité dérivée, bornes de couverture
- Avantages : la donnée brute est conservée ; la projection se recalcule ; Kavita, kosync et le lecteur parlent la même langue (un fichier).
- Inconvénients : une table de plus et une vue ; des bornes parfois inconnues.
### C. Écrire la même position sur toutes les Unités du support
- Avantages : simple.
- Inconvénients : faux dès le deuxième chapitre ; ne dit pas sur quel contenu la position a été prise.

## Décision
Option B (arbitrage G9 ; V1, V2).
- Ancre : `reading_position(user_id, export_item_id, content_hash, locator, percent, client_ts, server_ts, device_id?)`.
- Projection : `reading_progress(user_id, unit_id, state, last_position_id)` dérivée par `unit_coverage` ; lue par le suivi, « Continuer la lecture », le transfert d'Édition (EF-BIB-026) et le renvoi de l'état « lu » vers Suwayomi.
- `unit_coverage.start_locator?` et `end_locator?` dès M1a (index de page, href ou index de spine), remplis quand ils sont connus.
- `document_alias → export_item_id` (+ `content_hash`).
- Invariant **I30** (numéro libre suivant ; I23 est la langue) : une position ne s'applique qu'au contenu sur lequel elle a été prise ; sinon transposée, marquée `approximate`, journalisée.
- Fusion hors ligne (M5) : `client_ts` le plus récent, borné par `server_ts`, historique, conflit proposé (EF-LEC-007).

## Conséquences
- Positives : progression juste sur les tomes et les romans ; kosync sans 409 sur les tomes ; positions jamais appliquées à un contenu qui a changé sans le dire.
- Négatives : la projection doit être recalculée après une modification de couverture ; les bornes inconnues donnent une progression par tome seulement, signalée.

## Risque principal
Bornes de couverture fausses (ordre des entrées mal lu) qui marquent « lus » des chapitres non lus. Mitigation : `origin` et `confidence` sur `unit_coverage`, projection recalculable, test sur tomes réels synthétiques.

## Réversibilité
Haute : la projection est dérivée ; l'ancre est la donnée la plus riche et ne se perd pas.

## Amendement passe 10 (2026-11-03)
K6 (Y2).
- **Forme temporelle réservée** : `unit_coverage.start_locator`/`end_locator` et `reading_position.locator` acceptent une position temporelle au format des fragments temporels du W3C (`t=<début>,<fin>`, en secondes) pour l'audio ; aucun normaliseur ne suppose qu'un locator est un entier ou un CFI. Rien n'est implémenté avant une destination audio (EF-EXP-035).

## Exigences liées
EF-LEC-014, EF-LEC-007, EF-LEC-009, EF-BIB-026, EF-BIB-044, ENF-TIME-001.

## Sources
- `deliverables/review/explorateur-passe7.md` (V1, V2)
- `deliverables/review/ARBITRAGES-passe7.md` (G9, G10)
- https://github.com/Kareadita/Kavita/blob/develop/Kavita.Models/Entities/Progress/AppUserProgress.cs
- https://github.com/Kareadita/Kavita/blob/develop/Kavita.Services/KoreaderService.cs
- https://github.com/koreader/koreader/blob/master/plugins/kosync.koplugin/main.lua
- https://github.com/readium/architecture/tree/master/models/locators
- `deliverables/review/chasseur-trous-passe10.md`, `deliverables/review/explorateur-passe10.md`, `deliverables/review/ARBITRAGES-passe10.md` (passe 10)
- https://www.w3.org/TR/media-frags/

## Amendement passe 12 (2026-11-03)
M10 (AA5).

**Problème.** ADR-0012 et EF-BIB-045 prévoient de lire dans le lecteur (M5) une Œuvre **non exportée** depuis sa copie conservée (`content`), alors que `reading_position` exige un `export_item_id` et qu'I30 compare `export_item_id` + `content_hash` : une telle lecture n'aurait pas d'ancre.

**Options considérées.**
- 1. **Ancre polymorphe** `carrier_type ∈ {export_item, content}` + `carrier_id`, à l'image d'`unit_coverage` (retenue).
- 2. Créer l'`export_item` dès l'assemblage, indépendamment de tout export : cohérent avec « support indépendant de la destination », mais oblige à figer le chemin (I22) avant de connaître la destination, et crée des lignes d'export pour des contenus qui ne le seront peut-être jamais.

**Décision (option 1).** `reading_position(user_id, carrier_type, carrier_id, content_hash, locator, …)` ; I30 compare `(carrier_type, carrier_id, content_hash)` ; la projection `reading_progress` résout `content` comme `export_item` par `unit_coverage`. Les colonnes entrent dans le schéma M1a (tables vides jusqu'en M4/M5) : changement additif, sans perte. Le lecteur lit toujours le dérivé neutralisé, jamais l'original `untrusted` (I35, I45). Test `test_reading_position_on_unexported_content_anchor` (M5).

Source : `deliverables/review/chasseur-trous-passe12.md`, `deliverables/review/explorateur-passe12.md`, `deliverables/review/ARBITRAGES-passe12.md` (passe 12)

## Amendement passe 13 (2026-11-03)
N5 (GAP-13-12).

- La signature d'EF-LEC-014 est réécrite : `reading_position(user_id, carrier_type, carrier_id, content_hash, locator, percent, client_ts, server_ts, device_id?)`. `document_alias` reste clé par `export_item_id` seulement : kosync ne lit que des fichiers servis, un alias ne vise jamais un `content`.

Source : `deliverables/review/chasseur-trous-passe13.md`, `deliverables/review/explorateur-passe13.md`, `deliverables/review/ARBITRAGES-passe13.md` (passe 13)
