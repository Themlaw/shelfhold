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

- **httpx2** (BSD-3-Clause) : client HTTP sortant du fetcher (M1a) et du `TestClient` de
  Starlette, qui déprécie `httpx` ; repli : `httpx`. Le domaine ne l'importe jamais (contrat
  `domain-pure`).
- **argon2-cffi** (MIT) : hachage `argon2id` des mots de passe. **pynacl** (Apache-2.0) :
  XChaCha20-Poly1305 pour les secrets et les pierres tombales. **pyrage** (MIT/Apache-2.0) :
  flux age x25519 du corps d'une sauvegarde. L'enveloppe age/scrypt de la phrase secrète (export du
  trousseau, clé éphémère d'une sauvegarde) est écrite et lue par `secrets/age_scrypt.py` avec
  pynacl (ChaCha20-Poly1305) et la bibliothèque standard (scrypt, HKDF, HMAC) : aucune dépendance
  ajoutée, et un facteur scrypt fixe (pyrage le règle d'après une mesure de la machine, ce qui rend
  un fichier parfois illisible sous charge). Repli commun : `cryptography`.
- **defusedxml** (PSF-2.0) : seule porte d'entrée XML (`infrastructure/xml`, ENF-SEC-014) ; repli :
  lxml durci. **nh3** (MIT) : assainissement du HTML amont, derrière `sanitize_upstream_text`
  (ENF-SEC-015) ; repli : `bleach`. Les contrats `xml-single-entry` et `sanitize-single-entry`
  interdisent l'import de `xml`, `lxml`, `nh3` et `bleach` ailleurs que dans leur porte d'entrée.
- **apprise** (BSD-2-Clause) : notifications ; planchers de sécurité ENF-SEC-016.
  **Pillow** (MIT-CMU) et **python-multipart** (Apache-2.0) : planchers ENF-SEC-016, vérifiés dans
  le verrou par `test_dependency_floors_enforced_in_lockfile`.
- **pypdf** (BSD-3-Clause) : lecture stricte d'un PDF du dossier d'import (EF-SRC-012), version
  exacte `6.21.4` (publiée le 2026-10-28, plus de 14 jours). Importée par le seul worker de décodage
  (contrat `pypdf-confined`), donc dans un sous-processus borné en mémoire et en temps ; un PDF
  n'est rangé que si elle l'ouvre sans erreur, sans chiffrement, sans fichier joint.
- **webauthn** (py_webauthn, BSD-3-Clause) : passkeys (P8), plancher `>=3.0.1,<4` (3.0.1 publiée le
  2026-09-25) ; dépendances transitives verrouillées : cbor2 (MIT), cryptography (Apache-2.0 ou
  BSD-3-Clause), pyOpenSSL (Apache-2.0), pyasn1 et pyasn1-modules (BSD). Importée par le seul
  paquet `shelfhold.identity.webauthn` (contrat `webauthn-confined`) ; repli : `cryptography` et
  `cbor2` directement.
- **Contrats M1b (L00)** : `sablier-confined` (seul `integrations.sablier` appelle Sablier),
  `backup-dir-sole-writer` (seul `ops.backup` écrit le répertoire de sauvegarde, via
  `ops.backup.store`) et `third-party-plugins-single-loader` (seul `plugin_host` charge des
  plugins tiers : `importlib`, `pkgutil` et `runpy` sont interdits ailleurs, hors les lecteurs de
  ressources listés dans `ignore_imports`).
- **python-ulid** (MIT) : identifiants. **langcodes** (MIT) : `normalize_lang()` (ADR-0042) ; repli :
  table d'alias figée et versionnée. Contrat `lang-single-producer`.
- Développement : **hypothesis** (MPL-2.0), **syrupy** (MIT), **pytest-xdist** (MIT) et
  **xmlschema** (MIT) pour les suites `tests/property`, `tests/golden` et la validation de schémas.

### Exception à la fenêtre `exclude-newer`

Le verrou n'accepte que des publications vieilles de 14 jours (`exclude-newer`). Deux paquets font
l'objet d'une exception ciblée (`exclude-newer-package` dans `[tool.uv]`), parce que leur version
a été vérifiée explicitement :

- huey 3.5.4 (publiée le 2026-11-01), vérifiée par le spike S1 : plancher `>=3.5.4,<3.6`.
- apprise 2.1.0 (publiée le 2026-10-24), plancher d'ENF-SEC-016 : `>=2.1,<3`.

Retrait possible le 2026-11-15, quand les deux versions ont plus de 14 jours : supprimer la ligne
`exclude-newer-package` et relancer `uv lock`. Aucun autre paquet n'est concerné.

> **Note pour M1b L32** (M2 L00, 2026-11-10) : la date de levée (2026-11-15) n'est pas encore atteinte,
> la ligne est donc conservée. M1b L32 la retire à partir du 2026-11-15 (suppression de la ligne, puis
> `uv lock`) ; à défaut, le premier lot M2 qui touche à `backend/pyproject.toml` le fait.

- **tzdata** : base des fuseaux horaires (`Europe/Paris` pour la fenêtre de maintenance, ENF-OPS-014) ;
  l'image `python:slim` n'en contient pas et `zoneinfo` échouerait.
- **uvicorn**, **SQLAlchemy**, **Alembic**, **structlog**, **pydantic** : socle du squelette,
  versions minimales et verrou dans `backend/pyproject.toml` et `backend/uv.lock`.
