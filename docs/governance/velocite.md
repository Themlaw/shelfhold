# Vélocité

Source : [`docs/spec/03-roadmap-tdd.md`](../spec/03-roadmap-tdd.md) §1 bis, ENF-GOV-005, [ADR-0035](../adr/0035-decoupage-m1-et-budget-temps.md).

Le développement est fait par des agents d'IA orchestrés par le mainteneur. Les boîtes de temps en heures-homme de la spécification sont donc converties en **effort agent** (sessions, PR fusionnées, exigences fermées), suivi ici. L'hypothèse d'heures par semaine du porteur (Q34) est sans objet tant qu'il en est ainsi.

## Règles

1. Relever à chaque jalon : effort agent consommé, exigences (EF/ENF) fermées, PR fusionnées, temps de relecture du mainteneur.
2. La revue de jalon publie la vélocité et une projection du jalon suivant.
3. Règle de coupe : à 150 % de l'effort prévu, les éléments marqués « coupables » dans la section du jalon passent en « Plus tard », sans débat. Les garanties de cohérence, la sécurité, le contrôle parental et la surface d'exploitation ne sont jamais coupables.
4. Règle d'arrêt : voir [portes.md](portes.md).

## Relevé

| Jalon | Période | Effort agent | Exigences fermées | PR fusionnées | Relecture mainteneur | Remarques |
|---|---|---|---|---|---|---|
| M0 | | | | | | |
| M1a | 2026-11-04 à 2026-11-05 (PR #24 à #63, hors lot de documentation) | à relever par le mainteneur | 122 sur 123 couvertes avant le lot de documentation (`tools/traceability.py --milestone M1a`) ; le budget mémoire (ENF-OPS-005) est couvert par ce lot | 40 | à relever par le mainteneur | Plafond du jalon : 300 h d'effort agent ; règle de coupe à 150 %. Aucun élément coupable n'a été reporté à ce stade. |
| M1b | | | | | | |
| M2 | 2026-11-10 à 2026-11-12 (PR #115 à #172, en cours) | à relever par le mainteneur | 57 sur 58 couvertes au 2026-11-12 (`tools/traceability.py --milestone M2`) ; reste ENF-GOV-004 (second registre, hors lot de documentation) | 35 PR de lot (« M2 Lxx »), 55 PR fusionnées depuis le lot L00 | à relever par le mainteneur | Aucun élément coupable de M2 n'a été coupé (ADR-0035, amendement M2). Chiffres provisoires : la revue de fin de jalon les fige. |
| M3 | | | | | | |
| M4 | | | | | | |
| M5 | | | | | | |
| M6 | | | | | | |

## Dette de code mesurée

| Lot | Mesure | Valeur |
|---|---|---|
| M1b L31 (objection 15) | lignes de `backend/src` (hors tests), delta net | environ -255 (20 ajoutées, 274 supprimées) ; les aides déplacées vivent dans `backend/tests/support/` |
| M1b L31 | entrées de `production-reachability-allowlist.toml` | 47 symboles « until M1b L31 » et 5 « until M1b L20/L24/L25 » retirés ; 31 symboles regroupés par raison et par jalon (M2, M3, M4) |
| M3 L56 (dettes de M3) | lignes de `backend/src` (hors tests), delta net | -13 (0 ajoutée, 13 supprimées : `work_is_visible`, `M3_SERVED_STATES` et leurs exports) ; `backend/tests` : +36 / -35 (le test de garde, l'aide `work_is_visible` déplacée dans `tests/visibility_env.py`) ; `e2e/workshop` : +16 / -12 (liste d'écrans et mesure du défilement en commun) |
| M3 L56 | entrées de `production-reachability-allowlist.toml` | 19 entrées avant, 16 après ; plus aucune `until = "M3"` : 2 symboles supprimés, 1 test de route réécrit sur une vraie route, 1 test de route rendu permanent, 2 entrées redatées M5 (D9), 2 redatées M4 (`docs/decisions/M3.md`, section des dettes) |
