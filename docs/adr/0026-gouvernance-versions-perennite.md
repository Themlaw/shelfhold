---
status: accepté
date: 2026-11-02
amended: 2026-11-02 (passe 2) ; 2026-11-03 (passe 5)
decision-makers: mainteneur
---
# ADR-0026 — Gouvernance, politique de versions et pérennité

> **Amendée le 2026-11-02 (passe 2).** En cas de divergence, la section « Amendement passe 2 » prévaut sur le texte qui précède.

## Contexte et problème
Shelfhold est maintenu par une seule personne. Le chasseur de trous (GAP-1-14, -15, -16, -17) et la revue légale (OBJ-LEG-09, -12) ont relevé plusieurs absences :
- pas de successeur désigné, ni de procédure d'archivage ;
- pas de politique de divulgation de failles ;
- pas de procédure face aux notifications légales ;
- pas d'accès de secours administrateur ;
- pas de politique de versions ni de retour arrière.

Côté exploitation (OBJ-OPS-04, -05, -06), plusieurs risques ont aussi été signalés :
- le tag `:0` mis à jour automatiquement par un outil GitOps peut appliquer une migration de schéma non réversible ;
- une image ancienne démarrée sur un schéma plus récent corrompt les données.

Le précédent Readarr, projet mort laissant ses utilisateurs sans sortie, est cité dans le cahier (R4).

## Facteurs de décision
- Un facteur bus de 1 : le projet doit pouvoir s'arrêter proprement.
- Les mises à jour automatiques sont courantes en homelab (outils GitOps, Watchtower).
- Exposition légale d'un dépôt public touchant au contenu.
- Coût de gouvernance acceptable pour un mainteneur seul.

## Options considérées
### A. Gouvernance écrite minimale + organisation GitHub + politique de versions stricte
- Avantages : fin de vie et transmission prévues ; mises à jour automatiques sûres ; procédures prêtes avant un incident.
- Inconvénients : documents à rédiger et à tenir à jour ; discipline de release (pas de migration en patch).
### B. Compte personnel, versions `latest` et `:0`, gouvernance implicite
- Avantages : zéro effort initial.
- Inconvénients : un auto-update peut casser la production. En cas d'abandon, l'image et le dépôt sont orphelins. L'exposition personnelle du mainteneur est maximale.
### C. Fondation ou collectif dès le départ
- Avantages : pérennité maximale.
- Inconvénients : disproportionné avant la 1.0 ; aucune structure d'accueil évidente.

## Décision
Option A.

**Gouvernance (ENF-GOV-001)**
- Le dépôt vit dans une organisation GitHub gratuite. Cela donne accès à la merge queue et fixe l'image `ghcr.io/<org>/shelfhold`.
- `GOVERNANCE.md` documente :
  - le facteur bus ;
  - un successeur désigné, avec accès à GHCR, aux clés de signature et au domaine ;
  - une procédure d'archivage propre, qui renvoie vers l'export intégral (ADR-0023) ;
  - la politique de modération légale des issues.
- Contributions sous DCO, sans CLA. Commits signés avec l'e-mail `noreply`. 2FA matérielle.

**Sécurité et légal**
- `SECURITY.md` (ENF-SEC-011) : signalement privé via GitHub, délai de triage, versions supportées, avis GHSA.
- Procédure de réponse aux notifications légales :
  - accusé de réception sous 72 h ;
  - pas de contre-notification sans avis juridique ;
  - pas de réécriture d'historique ;
  - adresse abus dédiée.

**Accès de secours**
- `shelfhold admin reset-password` et connexion locale de secours (EF-USR-011).

**Versions et migrations (ENF-OPS-008)**
- Tags `X.Y` épinglables (`0.Y` avant la 1.0). L'auto-update ne suit que les patchs de la ligne `X.Y`.
- Aucune migration de schéma dans une release patch (contrôle CI).
- Les notes de version indiquent « migration oui/non, réversible oui/non ».
- `shelfhold migrate` est séparé du démarrage. Shelfhold refuse de démarrer sur un schéma plus récent que le sien.
- La procédure de retour arrière est testée en E2E. Les dépréciations sont annoncées au moins une version mineure à l'avance.

**Maintenance**
- Mode maintenance ou pause (EF-INT-018) pour les opérations lourdes.

## Conséquences
- Positives : mises à jour automatiques sans risque de corruption ; incidents légaux traités par procédure ; pérennité crédible.
- Négatives : environ une journée de rédaction en M0. Discipline de release à tenir. La personne désignée comme successeur doit accepter ce rôle.

## Risque principal
Les documents ne sont pas tenus à jour, ou le successeur n'est pas joignable le moment venu. Mitigations : revue annuelle inscrite dans `GOVERNANCE.md` et vérification CI de la présence des fichiers obligatoires.

## Réversibilité
Haute pour les documents. Moyenne pour la politique de tags, car la changer après la 1.0 impose de prévenir les utilisateurs.

## Amendement passe 2 (2026-11-02)
- **Registre des dépendances critiques** (N1, B17, ENF-GOV-002) : `docs/dependencies.md` dès M0 — Huey (91 % des commits par une personne), foliate-js (89 %), lncrawl (80 %), Kavita (71 %), kepubify (99 %), comicbox si retenu (99 %), Suwayomi (42 %) : rôle, licence, dernière release, repli, signal de déclenchement ; chaque dépendance critique derrière un adaptateur dédié ; revue trimestrielle.
- **Revue de chevauchement Kavita** à chaque jalon (ENF-GOV-003, voir ADR-0020).
- **Continuité en cas de retrait** (ADR-0029) : le successeur désigné reçoit aussi l'accès au second registre et au miroir.
- **Charge et financement** (N11) : politique de support et de triage dans `CONTRIBUTING.md` et `GOVERNANCE.md` ; aucun don avant un hôte fiscal (Open Collective Europe ou association loi 1901) ; fonctions payantes = Jamais.
- **Réputation** (N12) : `AI_POLICY.md` visible depuis le README ; démos uniquement sur contenus libres ; première release taguée tôt (délais d'ancienneté des annuaires).

## Amendement passe 3 (2026-11-02)
- **Version de Python et ABI des plugins** (GAP-3-30, ENF-OPS-018) : politique « Python N+1 un an avant la fin de vie de N » ; à la montée de version de l'image, les plugins tiers d'ABI incompatible (roues `cp313`) sont marqués « ABI incompatible » et une réinstallation guidée est proposée.
- **Aucune donnée propre à une installation dans le dépôt** (C14) : les plans de déploiement d'un exploitant (chemins, hôtes, scripts maison) vivent hors dépôt.

## Amendement passe 4 (2026-11-03)
Budget de temps (D1, R1) : `ROADMAP.md` porte l'hypothèse d'heures par semaine, l'estimation et la boîte de temps par jalon, la vélocité et la règle de coupe (ADR-0035) ; **aucune promotion publique avant 1.0**. Versions : `0.2` = M1a, `0.3` = M1b, `0.4` = M2. Dépréciation de l'API par en-têtes `Deprecation`/`Sunset` (ADR-0006). DCO et clause entrante (ADR-0036).

## Amendement passe 5 (2026-11-03)
T3, T7 (E14, E15). **Statut public** du projet en tête du README (`actif`, `maintenance seule`, `archivé`) et message type « pause annoncée » ; **règle d'arrêt** et **règle de passation** écrites dans `docs/governance/portes.md` (ADR-0038) : vélocité sous 50 % de l'hypothèse pendant 3 mois, ou deux portes en échec → maintenance seule (correctifs de sécurité 12 mois) puis archivage selon cet ADR ; un contributeur régulier (≥ 5 PR fusionnées en 6 mois) se voit proposer le rôle de co-mainteneur. `AI_POLICY.md` couvre aussi le code assisté par IA du mainteneur. Liste de veille élargie (Uchiyomi, Tranga, Mangarr, BookOrbit).

## Exigences liées
ENF-GOV-001, ENF-SEC-011, ENF-OPS-002, ENF-OPS-008, ENF-LEG-004, ENF-DOC-001, EF-USR-011, EF-INT-016, EF-INT-018, ENF-OPS-018, ENF-GOV-005, ENF-LEG-010, ENF-GOV-006, ENF-GOV-003.

## Sources
- https://docs.github.com/en/code-security/security-advisories/working-with-repository-security-advisories/configuring-private-vulnerability-reporting-for-a-repository
- https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/managing-a-merge-queue (organisations seulement)
- https://docs.github.com/en/site-policy/content-removal-policies/guide-to-submitting-a-dmca-takedown-notice
- https://developercertificate.org/
- https://github.com/Readarr/Readarr (projet retiré, précédent R4)
- https://docs.paperless-ngx.com/administration/#exporter
- `deliverables/review/chasseur-trous-passe1.md` (GAP-1-14→17), `contradicteur-ops-secu-legal.md` (OBJ-OPS-04→06, OBJ-LEG-09, -12), ARBITRAGES A24, A38
- https://devguide.python.org/versions/ *[mem]*
- `deliverables/review/explorateur-passe4.md` (R1, R2)
- `deliverables/review/ARBITRAGES-passe4.md`
- `deliverables/review/chasseur-trous-passe5.md`
- `deliverables/review/explorateur-passe5.md`
- `deliverables/review/ARBITRAGES-passe5.md`
