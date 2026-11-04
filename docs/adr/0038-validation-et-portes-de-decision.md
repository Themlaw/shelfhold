---
status: accepté
date: 2026-11-03
decision-makers: mainteneur
---
# ADR-0038 — Validation du besoin et portes de décision

## Contexte et problème
Après quatre passes, la roadmap a un budget, une vélocité et une règle de coupe (ADR-0035), et ADR-0026 prévoit l'archivage. Il manque trois choses (explorateur passe 5, T2–T4) :
- **La désirabilité n'est pas vérifiée.** Le seul contrôle par des utilisateurs est sommatif (test à 5 tâches dans la DoD de 1.0, ENF-UX-001), donc **après** les 240 à 420 h de M3. Personne n'a vérifié que des adultes non techniques ou des enfants demanderont des livres dans une UI d'atelier plutôt que par un message au propriétaire. Pendant ce temps, des projets voisins livrent des parcours de demande avec approbation : la partie « famille » est le segment le plus banalisé, le pont ne l'est pas.
- **Il n'y a ni go/no-go ni critère d'arrêt ou de passation.** Le coût déjà engagé pousse à continuer.
- **Aucune valeur n'est mesurée** avant la bascule : impossible de dire ensuite si Shelfhold vaut son coût.

## Facteurs de décision
- Coût quasi nul : quelques heures, pas de télémétrie (ENF-PRIV-001), pas de données nominatives dans le dépôt public.
- Décider **avant** les dépenses lourdes (fin de M0, après M1a, avant M3, avant l'annonce de 1.0).
- Questions écrites d'avance, avec la suite prévue en cas de « non ».

## Options considérées
### A. Rien de plus (test d'utilisabilité avant 1.0 seulement)
- Avantages : aucun travail supplémentaire.
- Inconvénients : jusqu'à ~400 h d'UI construites sans preuve de besoin ; aucun point d'arrêt explicite.
### B. Validation légère + portes P0–P3 + règles d'arrêt et de passation, écrites dans `docs/governance/portes.md`
- Avantages : relevé de départ et entretiens en M0 (~3 h), test « concierge » de 8 semaines pendant M1a/M1b (~2 h), prototype cliquable avant M3 (~4 h), indicateurs locaux (`shelfhold stats`) ; chaque porte a une question fermée et une suite.
- Inconvénients : petits effectifs (un foyer), donc signal faible ; discipline à tenir à chaque revue de jalon.
### C. Étude utilisateurs approfondie (panel, plusieurs foyers) avant M1a
- Avantages : signal plus fort.
- Inconvénients : coûteux, hors de portée d'un mainteneur seul ; M1a ne sert que l'admin et n'en a pas besoin.

## Décision
Option B (arbitrages E13, E14, E15).
- **M0** : relevé de départ (`docs/recherche/releve-depart.md`, agrégats) et entretiens de 30 min avec 2 ou 3 membres du foyer.
- **M1a/M1b** : test « concierge » de 8 semaines ; journal privé (date, adulte ou enfant, type, délai, lu ou non), agrégats seulement dans le dépôt.
- **Avant M3** : prototype cliquable de 3 écrans tirés des maquettes, 5 tâches, 3 personnes.
- **Portes** : P0 (fin de M0 : spikes, concurrent qui ferait déjà le pont), P1 (8 semaines après `0.2` : moins d'interventions manuelles que le relevé de départ, sans les anciens scripts ; sinon corriger en M1b ou revenir en arrière par `detach --restore-upstream`), **P2 (avant M3 : ≥ 2 demandes par mois et préférence pour l'UI ; sinon M3-lite)**, P3 (avant l'annonce : 2 h par semaine disponibles pour le support, Q27b rendu).
- **Règle d'arrêt** : vélocité sous 50 % de l'hypothèse pendant 3 mois consécutifs, ou deux portes en échec → « maintenance seule » (correctifs de sécurité pendant 12 mois, statut affiché dans le README), puis archivage selon ADR-0026.
- **Règle de passation** : un contributeur régulier (≥ 5 PR fusionnées en 6 mois) se voit proposer le rôle de co-mainteneur.
- **Mesure** : `shelfhold stats` (EF-INT-025), calculé localement, lisible par l'admin seul.

## Conséquences
- Positives : jusqu'à ~120 h évitées si la porte P2 conduit à M3-lite ; l'arrêt devient une décision prévue, pas un abandon silencieux ; la bascule est jugée sur des chiffres.
- Négatives : un signal tiré d'un seul foyer ; quelques heures de plus en M0 ; le seuil de P2 est arbitraire (Q37).

## Risque principal
Les portes sont écrites puis jamais relues. Mitigation : `portes.md` est relu à chaque revue de jalon avec le pré-mortem et la vélocité (ENF-GOV-003, ENF-GOV-005) ; le résultat de chaque porte est consigné dans le journal.

## Réversibilité
Haute : document de gouvernance ; M3-lite ne supprime rien du modèle ni des contrats (les EF reportées passent en Plus tard).

## Exigences liées
ENF-GOV-006, ENF-GOV-005, ENF-GOV-003, ENF-UX-001, EF-INT-025, EF-EXP-056, EF-USR-010, EF-UX-004, EF-UX-005, EF-UX-006, EF-INT-021.

## Sources
- `deliverables/review/explorateur-passe5.md` (T2, T3, T4, T5, T7)
- `deliverables/review/ARBITRAGES-passe5.md` (E13, E14, E15)
- https://github.com/bookorbit/bookorbit/releases
- https://github.com/AngeloSha/uchiyomi
