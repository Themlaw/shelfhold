# Origine des fixtures `openapi.json` de Kavita (A16)

Fichiers récupérés une seule fois, hors CI, par `gh api` (contenu brut de `openapi.json` à la
racine du dépôt public `Kareadita/Kavita`, au tag indiqué), puis réduits aux routes de
`docs/kavita-routes.md` par `tools/reduce_kavita_openapi.py` (routes, schémas atteints, schéma de
sécurité). Récupération faite le 2026-11-05. Licence du fichier d'origine : GPL-3.0 (Kavita) ;
seul le contrat d'API réduit est versionné ici.

| Fichier | Rôle | Tag | Commit du tag | Blob de `openapi.json` |
| --- | --- | --- | --- | --- |
| `0.9.1.json` | minimum de la plage (ENF-COMP-001) | `v0.9.1.0` | `289bc39b3e08f26cc14b8a8fd67b815ad9aaa15f` | `6dceaac0b825ff320810621a44af94c022d3f3c0` |
| `pinned.json` | version épinglée (`docs/dependencies.md`) | `v0.9.1.4` | `879b8b73eb41d66f88dde8eb874cc99a3397eb11` | `5d2a71cd25f89a774ece9549273d384e8f696d9d` |
| `latest.json` | dernière version stable au 2026-11-05 | `v0.9.1.4` | `879b8b73eb41d66f88dde8eb874cc99a3397eb11` | `5d2a71cd25f89a774ece9549273d384e8f696d9d` |

`pinned.json` et `latest.json` sont aujourd'hui identiques : la version épinglée est la dernière
stable. Le jour où elles divergent, `latest.json` est remplacé par le nouveau tag (la dérive
éventuelle ouvre une issue par le nightly, non bloquant). Le champ `info.version` du fichier du
tag `v0.9.1.4` dit `0.9.1.1` : Kavita ne le met pas à jour à chaque tag, le tag fait foi.
