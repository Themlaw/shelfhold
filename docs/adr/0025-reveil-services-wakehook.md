---
status: accepté
date: 2026-11-02
amended: 2026-11-03 (passe 6) ; 2026-11-04 (porteur 2)
decision-makers: mainteneur
---
# ADR-0025 — Réveil des services à la demande : port WakeHook (Sablier)

## Contexte et problème
Le propriétaire utilise Sablier pour arrêter les conteneurs inactifs. Suwayomi est le premier candidat : il consomme au moins 750 Mo et plus avec KCEF. La version 1 supposait que Shelfhold « réveillerait » Suwayomi, mais c'est faux (OBJ-OPS-09, 09-closure-B §7) :
- Sablier ne démarre un conteneur que lorsqu'un plugin de reverse proxy voit une requête, ou sur appel direct de son API ;
- Shelfhold appelle Suwayomi directement sur le réseau Docker, donc Sablier ne voit rien ;
- la session n'est renouvelée que par le trafic que Sablier observe ;
- les arrêts sont **inconditionnels** : il n'existe pas de crochet « occupé ».

Conséquence : un téléchargement de 40 chapitres peut être coupé net. Shelfhold, lui, porte des tâches planifiées. Il n'est pas candidat à la mise à zéro.

## Facteurs de décision
- Économiser la mémoire des services lourds sans perdre de jobs.
- Ne pas compter un réveil lent comme une panne de source.
- Rester optionnel : beaucoup d'installations n'ont pas Sablier.

## Options considérées
### A. Port `WakeHook` avec un adaptateur Sablier : réveil avant un lot, renouvellement pendant les jobs longs
- Avantages : explicite et testable. Les jobs ne sont pas coupés. Délai de réveil distinct. Fonctionne avec la stratégie `blocking`.
- Inconvénients : il faut connaître l'URL de Sablier et les noms ou le groupe des conteneurs. Encore un adaptateur à maintenir.
### B. Passer par le reverse proxy pour que Sablier voie le trafic
- Avantages : aucun code spécifique.
- Inconvénients : le trafic interne traverse le proxy (latence, authentification). La session expire quand même pendant les longs téléchargements si la fenêtre est courte.
### C. Interdire Sablier devant les services pilotés
- Avantages : simple.
- Inconvénients : perte de l'économie de mémoire voulue par le propriétaire.

## Décision
Option A, avec C comme valeur par défaut documentée : sans configuration, aucun réveil n'a lieu.
- Le port `WakeHook` (EF-INT-022) s'appuie sur l'adaptateur Sablier. Il prend une URL d'API, `names` ou `group`, `session_duration` et la stratégie `blocking`.
- Avant un lot, il appelle Sablier pour démarrer le service et attend qu'il soit prêt. Pendant un job long, il renouvelle la session toutes les `session_duration / 2`.
- Le délai de réveil est distinct du délai de requête. Les réessais pendant le réveil ne comptent pas comme des échecs de source.
- **Shelfhold lui-même n'est jamais derrière Sablier.** La documentation le dit explicitement. Les tâches périodiques manquées sont rattrapées au démarrage (EF-INT-003). Shelfhold n'utilise jamais l'OPDS ni kosync pour réveiller Suwayomi.
- Kavita derrière Sablier est déconseillé ; si c'est le cas, le même port réveille Kavita avant `scan-folder` et `plugin/parse`.

## Conséquences
- Positives : économie de mémoire préservée pour Suwayomi, sans téléchargements coupés.
- Négatives : un adaptateur de plus, et un simulateur Sablier dans les tests. Il faut documenter les noms de conteneurs.

## Risque principal
Évolution de l'API de Sablier. Mitigations : version épinglée dans la documentation, adaptateur isolé derrière le port, test de contrat contre un faux Sablier.

## Réversibilité
Haute : port optionnel, simple configuration.

## Amendement passe 3 (2026-11-02)
- **Vérification pilotée** (C5, EF-SRC-025) : réveiller Suwayomi avant un lot ne suffit pas, car un Suwayomi endormi n'exécute pas sa planification et son minuteur repart souvent du démarrage. Avec un WakeHook, Shelfhold réveille Suwayomi **et** lui demande le rafraîchissement des séries suivies ; sans WakeHook, l'utilisateur déclare Suwayomi « toujours actif » et Shelfhold ne fait que vérifier la fraîcheur. Test : Suwayomi endormi 24 h → le nouveau chapitre est vu au réveil suivant.

## Amendement passe 6 (2026-11-03)
GAP-6-18.
- Le `WakeHook` se rattache à **toute dépendance**, source ou destination : Kavita endormi par Sablier est réveillé avant `scan-folder`, l'audit d'EF-EXP-055 et la confirmation d'arrivée, avec le même délai de réveil distinct ; ligne correspondante dans la matrice de pannes (`wake.deferred`). Pour un service connecté, la capacité `wake` du port `ConnectedService` délègue au WakeHook (ADR-0041).

## Amendement porteur 2 (2026-11-04)
- **Cadence adaptative et repli** (`review/ARBITRAGES-porteur2.md`, Q3a, Q3c ; EF-SRC-037, EF-SRC-038) : en mode piloté, le réveil et le rafraîchissement d'une liaison sont planifiés à `source_binding.next_expected`, calculé depuis la cadence observée (bornes `follow.check_min` 1 h et `follow.check_max` 7 j) ; une série terminée ou en pause est vérifiée au plus tous les 30 jours ; les réveils sont **groupés** par instance Suwayomi (un réveil sert toutes les liaisons dues dans la fenêtre) pour ne pas multiplier les démarrages. Une liaison durablement en panne passe inactive et le suivi bascule sur la liaison de rang suivant, avec notification (`source.fallback`). En mode « toujours actif », la cadence ne sert qu'à la fraîcheur (`series.stale`).

## Exigences liées
EF-INT-003, EF-INT-022, EF-SRC-008, EF-SRC-009, ENF-OPS-005, ENF-OPS-007, ENF-OPS-011, EF-SRC-025, EF-SRC-033, EF-EXP-055.

## Sources
- https://github.com/sablierapp/sablier (v1.19.1, 2026-10-28)
- https://github.com/sablierapp/sablier/blob/main/docs/content/concepts/strategies.md
- https://github.com/sablierapp/sablier/blob/main/docs/content/features/activity.md
- https://github.com/sablierapp/sablier/blob/main/docs/content/features/running-hours.md
- https://github.com/sablierapp/sablier/blob/main/docs/content/features/verify-before-stopping.md
- `research/09-closure-B.md` §7, `deliverables/review/contradicteur-ops-secu-legal.md` (OBJ-OPS-09), ARBITRAGES A23
- `deliverables/review/chasseur-trous-passe3.md` (GAP-3-05)
