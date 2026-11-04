---
status: accepté
date: 2026-11-02
decision-makers: mainteneur
---
# ADR-0029 — Résilience à un retrait sur la forge (GitHub)

## Contexte et problème
Le code, les images GHCR, les releases, le futur index de plugins et le flux de mises à jour sont tous hébergés sur GitHub. La passe 1 a traité la **prévention** d'un retrait (ADR-0015 : aucune source livrée, aucun nom d'outil de contournement). Elle n'a pas traité la **continuité de service** si un retrait survient malgré tout (N3). La politique de retrait de GitHub prévoit que, les paquets étant immuables, **le paquet entier** est désactivé même si une partie seulement est visée ; les forks ne sont pas désactivés automatiquement. Un retrait ferait échouer `docker compose pull`, renverrait 404 sur l'index de plugins et figerait les familles sur leur version.

## Facteurs de décision
- Une famille ne doit pas perdre la capacité de mettre à jour à cause d'un hébergeur.
- Coût faible pour un mainteneur seul.
- Garder l'authenticité des artefacts indépendante de l'hôte (signatures).
- Ne pas ajouter de requête sortante par défaut (ENF-PRIV-001).

## Options considérées
### A. GitHub seul (situation de la passe 1)
- Avantages : rien à faire ; un seul endroit.
- Inconvénients : point unique de défaillance pour le code, les images et l'index.
### B. Second registre publié par la CI, miroir du dépôt, URL d'index configurable, vérificateur de mises à jour non bloquant
- Avantages : `docker compose pull` fonctionne en changeant un nom d'image ; la signature cosign/minisign authentifie le contenu quel que soit l'hôte ; coût = un job CI et un miroir.
- Inconvénients : deux jeux d'identifiants de registre ; dépôt miroir à surveiller.
### C. Quitter GitHub pour une forge auto-hébergée (Forgejo)
- Avantages : maîtrise totale.
- Inconvénients : perte de la merge queue, des runners arm64 gratuits, de la visibilité ; charge d'exploitation pour un mainteneur seul.

## Décision
Option B (arbitrage B19, ENF-GOV-004).
- La CI publie les images signées sur **GHCR et un second registre** (Docker Hub, ou registre Codeberg/Forgejo : choix ouvert, Q28) ; le compose de référence documente les deux noms.
- Le dépôt a un **miroir** hors GitHub (Codeberg ou Forgejo personnel), poussé par la CI (`publish-mirror`).
- `SHELFHOLD_PLUGIN_INDEX_URL` est configurable ; la signature de l'index et des wheels (ADR-0007, EF-SRC-017) authentifie le contenu indépendamment de l'hôte.
- Le vérificateur de mises à jour (`SHELFHOLD_UPDATE_CHECK_URL`) est **désactivé par défaut** et, s'il est activé, **non bloquant** : un échec produit une ligne de journal de niveau info, jamais une erreur.
- `GOVERNANCE.md` contient la procédure de continuité (bascule de registre, annonce par le miroir) ; la contre-notification reste soumise à un avis juridique (ENF-GOV-001).

## Conséquences
- Positives : un retrait ne fige plus les installations ; l'authenticité ne dépend pas de l'hôte.
- Négatives : second jeu de secrets CI ; quotas du second registre à surveiller (pull limits).

## Risque principal
Le second registre applique lui aussi un retrait. Mitigation : la signature permet d'ajouter un troisième emplacement (ou une image construite localement depuis le miroir) sans changer la chaîne de confiance.

## Réversibilité
Haute : un job CI et une variable ; retirer le second registre ne change rien au code.

## Exigences liées
ENF-GOV-001, ENF-GOV-004, ENF-PRIV-001, ENF-SEC-007, EF-SRC-017.

## Sources
- https://docs.github.com/en/site-policy/content-removal-policies/dmca-takedown-policy
- https://docs.github.com/en/site-policy/content-removal-policies/guide-to-submitting-a-dmca-takedown-notice
- https://alternativeto.net/news/2024/1/manga-reader-app-tachiyomi-ceases-development-amid-legal-threats-from-kakao-entertainment
- `deliverables/review/explorateur-passe2.md` (N3), `deliverables/review/ARBITRAGES-passe2.md` (B19)
