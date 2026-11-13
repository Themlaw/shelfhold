# Portes de décision

Source : [`docs/spec/03-roadmap-tdd.md`](../spec/03-roadmap-tdd.md) §1 ter, [ADR-0038](../adr/0038-validation-et-portes-de-decision.md), ENF-GOV-006. Relu à chaque revue de jalon avec le registre de pré-mortem ([pre-mortem.md](pre-mortem.md)).

## Portes

| Porte | Moment | Question | Si non |
|---|---|---|---|
| P0 | Fin de M0 | Les spikes S1, S2, S4 et S5 (dont `ui_login`) passent-ils dans leur boîte ? Un concurrent apparu fait-il déjà le pont sur une bibliothèque Kavita existante ? | Réduire à un outil en ligne de commande minimal, ou adopter le concurrent |
| P1 | 8 semaines après `0.2` (M1a) | Le pont tourne-t-il **sans les anciens scripts**, avec moins d'interventions manuelles par mois que le relevé de départ ? | Corriger dans M1b, ou revenir à l'automatisation précédente par `detach --restore-upstream` (testé) |
| P2 | Avant M3 | Le test concierge atteint-il au moins 2 demandes par mois sur 8 semaines, et au moins une personne préfère-t-elle l'interface au message (prototype) ? | M3-lite : profils, contrôle parental, notification au demandeur, recherche admin ; le parcours famille complet passe en « Plus tard » |
| P3 | Avant l'annonce de la 1.0 | Le mainteneur a-t-il encore 2 h par semaine pour le support public ? L'avis juridique Q27b est-il rendu ? | Pas d'annonce ; dépôt public « usage personnel, sans support » |

## Règle d'arrêt

À tout moment : vélocité sous 50 % de l'hypothèse pendant 3 mois consécutifs, **ou** deux portes en échec. Alors : mode « maintenance seule » (correctifs de sécurité pendant 12 mois, statut affiché en première ligne du README), puis archivage selon [ADR-0026](../adr/0026-gouvernance-versions-perennite.md) (dernière release et export intégral documenté).

## Règle de passation

Dès qu'un contributeur régulier existe (>= 5 PR fusionnées en 6 mois), proposer un rôle de co-mainteneur.

## Validation légère (ENF-GOV-006)

| Étape | Moment | Sortie |
|---|---|---|
| Relevé de départ | M0 | `docs/recherche/releve-depart.md` (agrégats seulement) ; **recherche documentaire** pour M0 |
| Entretiens courts | Dépend du porteur : **reportés**, non réalisés à ce jour | Notes privées ; agrégats dans `docs/recherche/` |
| Test « concierge » | 8 semaines pendant M1a/M1b | Journal privé ; agrégats publiés |
| Prototype cliquable | Avant M3 | Compte rendu formatif (ENF-UX-001) |
| Mesure | Dès M1a | `shelfhold stats`, sans télémétrie |

Décision M0 : la porte P0 est jugée sur S1, S2, S4, S5 et une veille concurrente documentaire.

## Journal des portes

| Porte | Date | Résultat | Notes |
|---|---|---|---|
| P0 | | à venir | |
| P1 | | à venir | |
| P2 | 2026-11-13 | franchie par délégation | Le porteur a délégué la décision (13/11). Le test concierge n'a pas été mesuré : aucun chiffre n'est consigné. Décision : M3 complet, parcours famille compris (déjà livré par L35, L41 et L44) ; `FAMILY_NAV_LITE` reste à `false`. À réexaminer au bilan de M3 si l'usage réel ne suit pas. |
| P3 | | à venir | |
