# Politique de sécurité

## Signaler une vulnérabilité

Ne publiez **pas** de vulnérabilité dans une issue publique. Utilisez le **signalement privé de GitHub** : onglet *Security* du dépôt, puis *Report a vulnerability*. Le mainteneur ouvre ensuite un avis (GitHub Security Advisory, GHSA) et publie un avis public après correctif.

Les notifications légales (retrait de contenu, abus) suivent la procédure de [GOVERNANCE.md](GOVERNANCE.md).

## Délais

Engagements du mainteneur, un bénévole : ce sont des cibles tenues **au mieux**, sans garantie contractuelle ni astreinte.

- **Accusé de réception** : ≤ 7 jours après le signalement privé.
- **Évaluation** : gravité et versions touchées communiquées au plus tard sous 30 jours.
- **Correctif** : visé ≤ 90 jours après le signalement, au mieux ; pour une vulnérabilité exploitée activement, la correction passe avant tout le reste. Un correctif plus long est annoncé au signalant avec ses raisons.
- **Divulgation coordonnée** : l'avis public est publié avec le correctif, ou au plus tard à l'échéance convenue avec la personne qui signale ; elle est créditée si elle le souhaite.

## Versions supportées

Seules la version mineure `X.Y` **courante** et la **précédente** reçoivent les correctifs de sécurité (par exemple 1.3 et 1.2 quand 1.3 est la dernière). Une version plus ancienne n'est pas corrigée : la mise à niveau est la réponse. Avant la 1.0, seule la dernière version est corrigée, et le projet est en pré-alpha.

## Avis publiés

Chaque vulnérabilité corrigée donne un **avis de sécurité GitHub (GHSA)**, public, avec les versions touchées, les versions corrigées, la gravité (CVSS) et un identifiant CVE quand il existe. Les avis sont aussi repris dans les notes de version et le `CHANGELOG.md`. La liste est dans l'onglet *Security*, puis *Advisories*, du dépôt.

## Vérifier ce que vous installez

Les images sont signées (cosign, sans clé) et accompagnées d'une provenance et d'un SBOM ; la commande de vérification, pour GHCR et pour le second registre quand il existe, est dans [`docs/admin/verifier-les-images.md`](docs/admin/verifier-les-images.md). Le dépôt est évalué chaque semaine par OpenSSF Scorecard (résultat dans l'onglet *Security* du dépôt).

## Intégrateurs

Un intégrateur commercial soumis au Cyber Resilience Act trouvera la posture du projet dans [`docs/legal/cra.md`](docs/legal/cra.md). Le projet ne prétend à aucune conformité : ce document décrit ce que le mainteneur fait, pas une certification.
