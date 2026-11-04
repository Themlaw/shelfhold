# Planchers de sécurité des dépendances

Exigence ENF-SEC-016. Les versions minimales sont imposées dans `backend/pyproject.toml` :
dépendance directe quand le paquet est importé, contrainte `uv` (`constraint-dependencies`)
quand il n'arrive qu'en transitif ou plus tard. Un nouvel avis relève le plancher par une release
de correctif. Les releases publiées depuis moins de 14 jours sont exclues au verrouillage
(`exclude-newer = "14 days"`).

| Paquet | Plancher | Avis | Mécanisme |
| --- | --- | --- | --- |
| starlette | >= 1.3.1 | CVE-2026-54283, CVE-2026-54282 | dépendance directe |
| python-multipart | >= 0.0.30 | CVE-2026-53539, CVE-2026-53538 | contrainte uv |
| Pillow | >= 13.0.0 | GHSA-wqgw-9xfh-qf2g, GHSA-xcvg-w56q-r89m | contrainte uv |
| apprise | >= 2.1, < 3 | CVE-2026-59180, GHSA-856c-92hv-3vxx | contrainte uv |
