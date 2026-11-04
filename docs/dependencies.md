# Dépendances critiques

Exigence ENF-GOV-002 (risque R21, ADR-0026). Une dépendance est **critique** quand son abandon
ou son changement de licence obligerait à réécrire une fonction du produit. Chacune vit derrière
**un adaptateur dédié** : le contrat import-linter `critical-*` de `backend/importlinter.ini`
interdit tout import hors de l'adaptateur. Le **facteur bus** est la part des contributions du
premier contributeur (API GitHub `contributors`, 100 premiers, relevé du 2026-11-04) ; la
colonne « Dernière release » donne le dernier tag publié à la même date.

Revue trimestrielle, assistée par Renovate (ENF-GOV-003). Un signal de déclenchement atteint
ouvre une issue `dependency-risk` et fait basculer vers le repli décrit.

| Dépendance | Rôle | Facteur bus | Licence | Dernière release | Repli | Signal de déclenchement | Adaptateur |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Huey | File de jobs SQLite (`SqliteHuey`, ADR-0005) | 92 % (coleifer) | MIT | 3.5.4 (2026-11-01) | Runner maison minimal sur SQLite, seulement après un nouvel ADR | 12 mois sans commit, changement de licence, dépôt archivé | `shelfhold/jobs/huey_app.py` (contrat `critical-huey`) |
| foliate-js | Moteur du lecteur intégré EPUB (M5) | 90 % (johnfactotum) | MIT | aucune release, sous-module épinglé sur un commit (dernier commit 2026-05-01) | Moteur EPUB alternatif derrière `ReaderEngine`, ou lecture déléguée à Kavita | 12 mois sans commit, changement de licence, dépôt archivé | Interface front `ReaderEngine` (`frontend/`, M5) |
| lncrawl | Source de romans via le méta-adaptateur (plugin natif, M2) | 80 % (dipu-bd) | GPL-3.0 | v4.14.0 (2026-08-03) | Sources natives Python écrites au cas par cas | 12 mois sans commit, changement de licence, dépôt archivé | `shelfhold/plugin_host/adapters/meta/lncrawl` |
| Kavita | Destination de l'export, service tiers et non une bibliothèque Python (M1a) | 71 % (majora2007) | GPL-3.0 | v0.9.1.4 (2026-09-02) | Destination `tree` (arborescence de fichiers) lisible par tout lecteur de bibliothèque | Rupture de l'API sur une route de la liste (`target.route_missing`), changement de licence, archivage | Adaptateur de destination Kavita (`shelfhold/export`, M1a) |
| kepubify | Conversion EPUB vers KEPUB, binaire externe (plus tard) | 99 % (pgaskin) | MIT | v4.0.4 (2022-03-09) | Étape de conversion désactivée ; export EPUB seul | 12 mois sans commit (déjà atteint : binaire figé, usage optionnel), changement de licence, archivage | Adaptateur de conversion (`shelfhold/export`, plus tard) |
| Suwayomi | Source connectée (serveur de téléchargement CBZ, M1a) | 43 % (AriaMoradi) | MPL-2.0 | v2.4.2366 (2026-09-30) | Sources natives (`plugin_host/adapters/native`) ; contrat S5 hebdomadaire pour détecter la rupture | Échec du contrat S5, retrait de la route de téléchargement REST sans équivalent GraphQL, changement de licence, archivage | `shelfhold/plugin_host/adapters/meta/suwayomi` |
| comicbox | Lecture et écriture de ComicInfo, si retenu (M1a) | 99 % (ajslater) | LGPL-3.0 | v5.5.1 (2026-11-02) | Génération ComicInfo.xml par `defusedxml` et un modèle interne | 12 mois sans commit, changement de licence, archivage | `shelfhold/library/adapters/comicinfo` (contrat `critical-comicbox`) |

## Contrats d'import

- Les dépendances Python (Huey, comicbox) ont un contrat `forbidden` actif dès M0 : aucun module
  de `shelfhold` ne les importe, sauf l'adaptateur nommé ci-dessus. L'adaptateur est ajouté à
  `ignore_imports` du contrat le jour où il est écrit.
- Les services tiers (Kavita, Suwayomi), le binaire kepubify et le composant front foliate-js ne
  s'importent pas en Python ; leur confinement est vérifié par les contrats « aucun symbole
  `suwayomi` ou `lncrawl` hors de son adaptateur » (EF-SRC-033) à l'écriture de chaque adaptateur.
- FastAPI et Starlette sont confinés à `shelfhold.interfaces` (contrat
  `fastapi-only-in-interfaces`).

## Autres dépendances notables

Non critiques (remplaçables sans réécriture), listées pour mémoire :

- **httpx2** (BSD-3-Clause, dépendance de développement) : client HTTP du `TestClient` de
  Starlette, qui déprécie `httpx` ; repli : `httpx`. Il n'est pas importé par le code de
  l'application.
- **uvicorn**, **SQLAlchemy**, **Alembic**, **structlog**, **pydantic** : socle du squelette,
  versions minimales et verrou dans `backend/pyproject.toml` et `backend/uv.lock`.
