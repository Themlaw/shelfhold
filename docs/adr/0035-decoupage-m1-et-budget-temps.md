---
status: accepté
date: 2026-11-03
amended: 2026-11-03 (passe 5) ; 2026-11-03 (passe 6) ; 2026-11-03 (passe 7)
decision-makers: mainteneur
---
# ADR-0035 — Découpage de M1 (M1a / M1b) et budget de temps par jalon

## Contexte et problème
Après trois passes de revue, M1 portait 107 exigences : le pont Suwayomi → Kavita, l'adoption, les garanties de cohérence (journal d'intentions, verrous, instance unique, sentinelle), le contrôle parental, la mise à l'abri, la sauvegarde, le mode piloté de Suwayomi, le second registre. L'explorateur de la passe 4 (R1) estime la roadmap entière à environ 1 100–1 800 h et la 1.0 à environ 650–1 100 h. Avec 6 à 10 h effectives par semaine pour un seul mainteneur, M1 en un bloc n'aurait remplacé l'automatisation existante qu'après 8 à 18 mois. C'est la première cause d'échec du pré-mortem (R4) : l'épuisement avant toute valeur. Aucun livrable ne donnait d'estimation, de budget ni de règle pour couper.

## Facteurs de décision
- Livrer tôt une chose utile (objectif O9) : remplacer l'automatisation existante sans perte de données.
- Ne jamais reporter ce qui coûterait une migration de données une fois le manifeste rempli (garanties de cohérence, identité).
- Une règle de coupe écrite d'avance, appliquée sans débat.
- Retarder la charge de maintenance publique (issues, support) tant que le produit n'est pas à 1.0.

## Options considérées
### A. Garder M1 en un bloc, sans budget
- Avantages : rien à replanifier.
- Inconvénients : premier usage réel très tardif ; aucune alerte avant le dépassement ; risque d'abandon.
### B. M1a (pont minimal avec garanties de cohérence, boîte de 150 h) + M1b (reste), effort estimé par jalon, vélocité, règle de coupe à 150 %, pas de promotion avant 1.0
- Avantages : un pont utilisable en 3 à 7 mois ; dépassement visible tôt ; les coupes sont décidées d'avance.
- Inconvénients : une version de plus ; chaîne de dépendances à vérifier (une exigence M1a ne doit dépendre de rien de plus tardif) ; estimations grossières.
### C. Réduire tout le périmètre V1 (abandonner M2 ou une partie de M3)
- Avantages : 1.0 plus proche.
- Inconvénients : contredit le positionnement « atelier » (ADR-0020) ; ne règle pas le délai avant le premier usage.

## Décision
Option B (arbitrage D1).
- **M1a — pont minimal** (`v0.2.0`) : adoption d'une bibliothèque Kavita adulte, suivi Suwayomi en mode « toujours actif », mode passage, une seule Variante exportée, export Kavita complet (nommage, ComicInfo, scan ciblé, confirmation par polling), **toutes les garanties de cohérence**, socle d'authentification et clés hachées (ADR-0018), surface d'exploitation (EF-INT-023), texte amont assaini (ENF-SEC-015). 77 exigences dont M1a est le premier jalon (49 EF, 28 ENF). **Boîte de temps : 150 h.**
- **M1b — pont complet** (`v0.3.0`) : contrôle parental et écran « À classer », copie conservée et « Mettre à l'abri » avec quota, Suwayomi piloté, sonde fonctionnelle et relance bornée, `follow`/`unfollow`, sauvegarde intégrée, second registre et miroir, benchmarks. 30 exigences dont c'est le premier jalon (21 EF, 9 ENF). Boîte : 150 h.
- **Effort estimé** (`03-roadmap-tdd.md` §1 bis) : M0 80–140 h ; M1a 120–170 h ; M1b 110–180 h ; M2 100–160 h ; M3 240–420 h ; 1.0 ≈ 650–1 070 h ; M4 170–250 h ; M5 170–250 h ; M6 120–200 h ; total ≈ 1 110–1 770 h. Taxe de maintenance : 1 à 2 h par semaine avant 1.0, 2 à 4 h ensuite.
- **Vélocité** : heures et EF fermées par semaine dans `docs/governance/velocite.md`, publiées à chaque revue de jalon avec une projection.
- **Règle de coupe** : à 150 % de la boîte de temps, les EF « coupables » listées dans la section du jalon passent en « Plus tard » ou au jalon suivant ; le journal l'enregistre. Jamais coupables : garanties de cohérence, sécurité, contrôle parental, surface d'exploitation.
- **Pas de promotion publique avant 1.0** : dépôt public non annoncé, aide en Discussions seulement.
- Hypothèse d'heures par semaine inscrite dans `ROADMAP.md` en M0 (Q34).

## Conséquences
- Positives : un premier usage réel plus tôt ; le budget devient un critère de revue ; les coupes ne se négocient pas au dernier moment ; la chaîne de dépendances est corrigée (`03` §1 bis).
- Négatives : une version et une revue de jalon de plus ; certaines exigences ont deux volets (M1a / M1b) ; les estimations restent grossières.

## Risque principal
Les estimations sont fausses d'un facteur 2. Mitigation : vélocité mesurée dès la première semaine de M1a, projection révisée à chaque revue, règle de coupe automatique.

## Réversibilité
Haute : c'est un découpage de planification. Fusionner M1a et M1b ou déplacer une exigence ne touche ni le schéma ni les contrats.

## Amendement passe 5 (2026-11-03)
Arbitrage E10 (GAP-5-10).
- **Intouchables M1a** (jamais reportés) : sentinelle (EF-EXP-029), journal d'intentions (EF-EXP-041), verrous et plans périmés (EF-EXP-042), instance unique et bail (ENF-OPS-015), écriture atomique (EF-EXP-006), autre écrivain (EF-EXP-043), adoption sans déplacement (EF-EXP-028), garde disque (ENF-OPS-006), socle d'authentification admin (EF-USR-021).
- **Coupables M1a** (seuls reportables à 150 %) : EF-EXP-032 réduite, EF-SRC-021 réduite, EF-INT-001 (`ETag`/`?since=`), ENF-COMP-001 (`known_bad_versions`), EF-EXP-034 réduite à un scan, EF-UX-002 (sonde lue par Kavita), EF-EXP-014, ENF-PERF-003, EF-INT-025, volet `--pair-auto` d'EF-EXP-038.
- **Boîte ré-estimée de bas en haut** : 90 exigences dont M1a est le premier jalon (60 EF, 30 ENF), effort 170–250 h, **boîte 200 h**, seuil de coupe 300 h (`03-roadmap-tdd.md` §1 bis). Une exigence absente des deux listes n'est pas coupée : au-delà de 150 %, c'est la boîte qui est revue (Q36).
- Les **portes de décision** et la **règle d'arrêt** complètent la règle de coupe (ADR-0038).

## Amendement passe 6 (2026-11-03)
Passe 6 (F1–F12).
- Les ajouts de la passe 6 en M1a sont du **schéma et des contrats** (`export_item`, `unit_coverage`, `replica`, `contributor_name`, `text_version`, langues canoniques, racines physiques, `adopt_ro`, port `ConnectedService`, transport et `egress`), estimés à 20–30 h, essentiellement tables, déclarations et tests de propriétés. La boîte de **200 h** est conservée : les ajouts sont absorbés par la marge de l'estimation 170–250 h, la règle des 150 % reste inchangée (Q42).
- **Intouchables** : EF-EXP-059 (sentinelle et écriture atomique par racine physique) rejoint la liste, qui compte 10 exigences pour 8 garanties. Les coupables sont inchangés.

## Amendement passe 7 (2026-11-03)
G1–G10.
- Ajouts passe 7 à M1a : 7 exigences de schéma et de contrat (EF-BIB-048, EF-BIB-049, EF-EXP-062, EF-SRC-034, EF-INT-026, EF-LEC-014, ENF-OPS-021), estimées 12 à 20 h ; M1a compte **110 exigences** (76 EF, 34 ENF). La boîte de 200 h et le seuil de 300 h sont conservés (Q44).
- **EF-INT-026 rejoint les intouchables** (11 exigences, 8 garanties) : c'est le comportement de la sentinelle et du journal d'intentions après une restauration, dont le sens doit être fixé avant le premier déploiement. Les coupables sont inchangés.

## Exigences liées
ENF-GOV-005, ENF-GOV-003, ENF-GOV-001, EF-USR-021, EF-INT-023, ENF-SEC-015, EF-EXP-002, EF-EXP-028, EF-SRC-025, ENF-QUAL-006, ENF-GOV-006, EF-INT-024, EF-EXP-051, EF-EXP-056, EF-EXP-059, EF-INT-026.

## Sources
- `deliverables/review/explorateur-passe4.md` (R1, R4)
- `deliverables/review/chasseur-trous-passe4.md` (§1, GAP-4-14)
- `deliverables/review/ARBITRAGES-passe4.md` (D1)
- `deliverables/review/chasseur-trous-passe5.md`
- `deliverables/review/explorateur-passe5.md`
- `deliverables/review/ARBITRAGES-passe5.md`
- `deliverables/review/chasseur-trous-passe7.md`, `deliverables/review/explorateur-passe7.md`, `deliverables/review/ARBITRAGES-passe7.md` (passe 7)
