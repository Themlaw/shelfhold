---
status: accepté
date: 2026-11-02
amended: 2026-11-02 (passe 1) ; 2026-11-02 (passe 2) ; 2026-11-03 (passe 10) ; 2026-11-03 (passe 11) ; 2026-11-03 (passe 12)
decision-makers: mainteneur
---
# ADR-0016 — CI/CD GitHub Actions et chaîne d'approvisionnement

> **Amendée le 2026-11-02 (passes 1 et 2).** En cas de divergence, la section « Amendement passe 1 » prévaut sur le texte d'origine, conservé pour l'historique.

## Contexte et problème
Les images sont construites par la CI et publiées sur GHCR ; le serveur ne tire qu'un compose minimal (outil de déploiement GitOps). Un mainteneur seul doit publier des images multi-arch fiables, signées et traçables. En mars 2026, l'action `trivy-action` a été compromise : 76 de ses 77 tags ont été réécrits (CVE-2026-33634).

## Facteurs de décision
- Gratuité sur dépôt public ; builds amd64 + arm64 natifs.
- Traçabilité (SBOM, provenance, signature).
- Défense contre les dépendances compromises.
- Releases maîtrisées par le mainteneur.

## Options considérées
### A. Runners natifs amd64/arm64, push-by-digest + manifest, GHCR, attestations + cosign, Grype, CodeQL, zizmor, Scorecard, actions épinglées par SHA, Renovate (`minimumReleaseAge`), release-please, rulesets
- Avantages : modèle Paperless (`ci-docker.yml`) et Immich ; runners arm64 GA pour dépôts publics (août 2025) ; vérifiable via `gh attestation verify`/`cosign verify`.
- Inconvénients : nombreux workflows à maintenir.
### B. Build QEMU multi-arch + Dependabot + semantic-release
- Avantages : plus simple à configurer.
- Inconvénients : QEMU lent ; les groupes wildcard Dependabot peuvent contourner le cooldown ; semantic-release publie à chaque merge.
### C. Build local + push manuel
- Avantages : aucune CI.
- Inconvénients : aucune traçabilité, non reproductible.

## Décision
Option A (D27, D28).
- **Tags** : semver (`X.Y.Z`, `X.Y`, `X`), `latest` (releases stables), `edge` (main), `sha-<long>`, `pr-N` (sans push pour les forks).
- **Sécurité des workflows** : `permissions` minimales, jamais de `pull_request_target` sur du code non fiable, un job agrégé `ci-success` comme check requis.
- **Dépôt** : immutable releases ; Grype plutôt que Trivy.
- **Releases** : release-please (PR de release, que le mainteneur déclenche quand il veut) ; Conventional Commits imposés sur les titres de PR.
- **Docs** : Zensical sur Pages.
- **Déploiement** : Déploiement par outil GitOps sur un tag majeur ou mineur ; mise à jour automatique seulement pour les patchs.
- Merge queue uniquement si le dépôt appartient à une organisation (non confirmé pour un compte personnel).

## Conséquences
- Positives : images traçables et signées ; dépendances retardées de 3–7 jours ; publication maîtrisée.
- Négatives : courbe d'apprentissage des workflows ; maintien des pins SHA (Renovate `helpers:pinGitHubActionDigests`).

## Risque principal
Compromission d'une action ou d'une dépendance. Mitigation : pins SHA, `minimumReleaseAge`, uv `exclude-newer`, zizmor, Scorecard.

## Réversibilité
Haute : workflows et outils interchangeables.

## Amendement passe 1 (2026-11-02)
- **Paliers de CI** (A16) :
  - **M0** : lint/type/tests, images natives amd64 + arm64, GHCR, actions épinglées par SHA, Renovate avec cooldown, release-please, rulesets.
  - **M1** : attestations de provenance + SBOM, Grype, CodeQL, zizmor.
  - **M3** : cosign, OpenSSF Scorecard, site de docs.
  - **Abandonnés** : nightly Postgres (ENF-PORT-002 retirée), mutation testing obligatoire (devenu optionnel, mensuel), benchmarks nocturnes (remplacés par une mesure avant release).
- **Merge queue** (09-closure-B) : réservée aux dépôts d'**organisation** ; les rulesets fonctionnent sur un dépôt personnel public. **Décision : dépôt sous le compte du porteur** (organisation GitHub gratuite plus tard, voir ADR-0019 ; pas de merge queue d'ici là), e-mail de commit `noreply`, 2FA matérielle (ENF-GOV-001).
- **Cooldown** : Dependabot applique 3 jours par défaut, **pas aux mises à jour de sécurité** ; Renovate n'a **pas** de `minimumReleaseAge` par défaut → `config:best-practices` + `security:minimumReleaseAgePypi` (3 jours), relevé à **7 jours** pour toutes les dépendances.
- **Tags** (A24, ENF-OPS-008) : on recommande d'épingler `X.Y` (`0.Y` avant 1.0) ; mise à jour automatique sur les patchs seulement ; pas de variante `:full` publiée en V1 (ENF-PERF-002).
- **Politique de divulgation** : SECURITY.md + signalement privé GitHub + GHSA dès M0 (ENF-SEC-011).

## Amendement passe 2 (2026-11-02)
- **Second registre** (ADR-0029, B19) : le workflow image publie aussi sur un second registre (choix Q28) ; signature cosign identique ; le compose documente les deux noms.
- **Miroir du dépôt** : workflow `publish-mirror` (palier M1) vers Codeberg ou un Forgejo personnel.
- Dépendances critiques suivies dans `docs/dependencies.md`, revue trimestrielle avec le tableau de bord Renovate (ENF-GOV-002).

## Amendement passe 4 (2026-11-03)
Limites de la CI gratuite (D13, R6) : cache des couches Docker **dans le registre** (`type=registry`) plutôt que dans le cache Actions ; SBOM, attestations et preuves de release attachées en **assets de release** ; nettoyage GHCR limité aux tags `pr-*`, jamais aux digests non étiquetés (manifestes de plate-forme, signatures, attestations) ; budget de durée par PR ≤ 15 min ; publication du SDK par **publication de confiance OIDC** en M6 (aucun jeton longue durée) ; contrôle **DCO** dès M0 (ADR-0036). Paliers : CodeQL, zizmor, attestations et second registre passent de « M1 » à **M1b**.

## Amendement passe 10 (2026-11-03)
K8 (Y5).
- Planchers de sécurité des dépendances (ENF-SEC-016) : `Pillow>=13.0.0`, `starlette>=1.3.1`, `python-multipart>=0.0.30` ; la CI échoue si le fichier de verrouillage descend sous un plancher ; une table datée dans `docs/security/dependances.md` donne l'avis à l'origine de chaque plancher.
- Règle de revue du front : ni *remote functions* ni `router.resolution: 'server'` (les avis SvelteKit du 2026-10-29 ne touchent pas une SPA en adapter-static).
- Le passage au plancher suivant après un nouvel avis se fait par une release de correctif, sans attendre un jalon.

## Amendement passe 11 (2026-11-03)
L3, L6 (GAP-11-04, GAP-11-15, Z1, Z4).
- **Job de contrat Suwayomi** (nightly sur la CI publique, ENF-LEG-013) : Suwayomi n'a que la **source locale**, alimentée par les CBZ synthétiques du dépôt ; **liste des dépôts d'extensions vide**, vérifiée par une assertion au démarrage ; **réseau sortant coupé** (seul le registre d'images épinglé est joignable pendant le tirage, puis réseau interne du job) ; assertions dans le job (`test_contract_job_has_no_extension_repo_and_no_egress`, `test_contract_suwayomi_uses_local_source_only`). Un workflow public qui installerait une extension serait visible de tous dans les journaux d'Actions.
- **Planchers** (ENF-SEC-016) : `apprise>=2.1,<3` (CVE-2026-59180 ; API v2) ; Komga ≥ 1.22.0 (CVE-2025-52880) dans `known_bad_versions` le jour où la destination est écrite (M4).
- **Frontière SDK MIT** (ENF-LEG-010) : contrôles CI dès M0 — `sdk/` avec `LICENSE` MIT et en-têtes SPDX par fichier (`test_sdk_files_have_mit_spdx_header`), contrat import-linter « le SDK n'importe jamais le cœur » (`test_sdk_never_imports_core`).

## Exigences liées
ENF-SEC-006, ENF-SEC-007, ENF-SEC-011, ENF-PORT-001, ENF-PERF-002, ENF-DOC-001, ENF-QUAL-001, ENF-OPS-008, ENF-GOV-001, ENF-QUAL-007, ENF-LEG-010, ENF-SEC-016, ENF-LEG-013, EF-SRC-008.

## Sources
- https://github.com/paperless-ngx/paperless-ngx/blob/dev/.github/workflows/ci-docker.yml
- https://github.com/immich-app/immich/blob/main/.github/workflows/docker.yml
- https://github.blog/changelog/2025-08-07-arm64-hosted-runners-for-public-repositories-are-now-generally-available/
- https://github.blog/changelog/2025-10-28-immutable-releases-are-now-generally-available/
- https://github.blog/changelog/2024-06-25-artifact-attestations-is-generally-available/
- https://wiz.io/blog/trivy-compromised-teampcp-supply-chain-attack
- https://www.aquasec.com/blog/trivy-supply-chain-attack-what-you-need-to-know
- https://nesbitt.io/2026/03/04/package-managers-need-to-cool-down.html
- https://dev.classmethod.jp/articles/dependabot-wildcard-group-cooldown-trap/
- https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/managing-a-merge-queue
- https://github.com/github/docs/blob/main/data/reusables/gated-features/merge-queue.md
- https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets
- https://docs.github.com/en/code-security/dependabot/working-with-dependabot/dependabot-options-reference#cooldown-
- https://docs.renovatebot.com/key-concepts/minimum-release-age/
- https://github.com/renovatebot/renovate/blob/main/lib/config/presets/internal/security.preset.ts
- `deliverables/review/explorateur-passe4.md` (R6)
- https://github.blog/changelog/2025-09-29-new-date-for-enforcement-of-cache-eviction-policy/
- https://github.blog/changelog/2025-11-20-github-actions-cache-size-can-now-exceed-10-gb-per-repository/
- `deliverables/review/ARBITRAGES-passe4.md`
- `deliverables/review/chasseur-trous-passe10.md`, `deliverables/review/explorateur-passe10.md`, `deliverables/review/ARBITRAGES-passe10.md` (passe 10)
- https://github.com/python-pillow/Pillow/security/advisories/GHSA-wqgw-9xfh-qf2g
- https://github.com/Kludex/starlette/security/advisories/GHSA-82w8-qh3p-5jfq
- https://github.com/Kludex/python-multipart/security/advisories/GHSA-5rvq-cxj2-64vf
- https://github.com/sveltejs/kit/security/advisories/GHSA-rqp5-r3wg-vg35
- `deliverables/review/chasseur-trous-passe11.md`, `deliverables/review/explorateur-passe11.md`, `deliverables/review/ARBITRAGES-passe11.md` (passe 11)

## Amendement passe 12 (2026-11-03)
M9, GAP-12-10.

- Nightly Kavita sur la matrice **{minimum 0.9.1, version épinglée, dernière}** : minimum et épinglée bloquants pour la release, dernière non bloquante (issue) ; Kavita amorcé sans interface (création de l'admin, puis clé dédiée), fixture par version.
- Test hors ligne en CI : chaque route de `docs/kavita-routes.md` existe dans l'`openapi.json` figé de chaque version de la plage (fixtures `tests/fixtures/kavita/openapi/<version>.json`, téléchargées au tag et versionnées).
- Nightly Suwayomi : extension de test synthétique construite dans le job, serveur de fixtures interne, réseau sortant coupé (ENF-LEG-013).

Source : `deliverables/review/chasseur-trous-passe12.md`, `deliverables/review/explorateur-passe12.md`, `deliverables/review/ARBITRAGES-passe12.md` (passe 12)
