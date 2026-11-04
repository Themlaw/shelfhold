# Registre de pré-mortem

Source : [`docs/spec/03-roadmap-tdd.md`](../spec/03-roadmap-tdd.md) §1 bis et §1 ter, R4, [ADR-0035](../adr/0035-decoupage-m1-et-budget-temps.md). Question posée : « le projet a échoué dans un an : pourquoi ? ». Relu à chaque revue de jalon.

| N° | Cause d'échec imaginée | Signal précoce | Mesure en place | Dernière revue | Statut |
|---|---|---|---|---|---|
| 1 | Le périmètre dépasse la capacité : rien d'utile avant longtemps | Vélocité sous l'hypothèse ; jalon > 150 % de sa boîte | Découpage M1a/M1b, règle de coupe, [velocite.md](velocite.md) | | ouvert |
| 2 | Le besoin du foyer n'est pas confirmé | Moins de 2 demandes par mois au test concierge | Porte P2, repli M3-lite ([portes.md](portes.md)) | | ouvert |
| 3 | Kavita, Komga ou Suwayomi livrent la fonction | Notes de version | Revue de chevauchement à chaque jalon | | ouvert |
| 4 | Une dépendance critique est abandonnée ou casse l'API | Contrat nightly en échec | Versions épinglées, registre `docs/dependencies.md`, adaptateurs | | ouvert |
| 5 | Retrait du dépôt ou de l'image par l'hébergeur | Notification | Posture légale ([ADR-0015](../adr/0015-posture-legale.md)), continuité ([ADR-0029](../adr/0029-resilience-retrait-forge.md)) | | ouvert |
| 6 | Mainteneur unique indisponible (facteur bus 1) | Absence prolongée | Successeur à désigner ([GOVERNANCE.md](../../GOVERNANCE.md)), archivage propre | | ouvert |
| 7 | Code d'agent de mauvaise qualité mal détecté | Régressions après fusion | TDD, relecture croisée, CI, relecture du mainteneur à chaque jalon ([AI_POLICY.md](../../AI_POLICY.md)) | | ouvert |
