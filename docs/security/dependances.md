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
| pyasn1 | >= 0.6.5 | GHSA-9m75-vc3g-4x39, GHSA-fm9c-5rcf-2q5p, GHSA-2qm8-x737-gmw5, GHSA-mm57-4v3p-rchx | contrainte uv |

## État des alertes de l'image (2026-11-12)

- **oauthlib** (GHSA-3jp2-3mrg-2vxc) : corrigé en 4.0.1, publié le 2026-10-31. Cette version a
  moins de 14 jours, donc `exclude-newer` l'exclut ; aucune exception n'est ajoutée. Elle devient
  éligible le 2026-11-14 : `uv lock --upgrade-package oauthlib` et plancher `>=4.0.1` à ce moment.
- **Paquets Debian** : le digest de `python:3.13-slim` est monté (apt et libapt-pkg 3.0.4, ce qui
  corrige CVE-2026-109518). zlib1g (CVE-2026-76844, correctif `1:1.3.dfsg+really1.3.1-1+deb13u1`)
  n'est pas encore dans l'image de base. libc6, openssl, libssl3t64, libsqlite3-0, libacl1,
  coreutils et dash n'ont pas de correctif Debian publié. Pas d'`apt-get upgrade` dans l'image
  (build non reproductible) : on attend l'image de base suivante, via le digest épinglé.
