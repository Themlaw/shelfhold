# Spike S5 — contrat Suwayomi (ADR-0008, ENF-LEG-013)

Mesuré le 2026-11-04 contre `ghcr.io/suwayomi/suwayomi-server@sha256:1fac27c387dc2ea9949e1a01ad7f6d6de9d72d3988338a95476748e5c45c4582`
(**v2.4.2366**, stable du 2026-09-30, soit 35 jours). Banc : `e2e/suwayomi/compose.yaml` (réseau `internal: true`, aucun
port publié, `EXTENSION_STORES=[]`, source locale seule, CBZ synthétiques), tests `backend/tests/contract/suwayomi/`
(`pytest -m contract`), workflow `.github/workflows/contract.yml` (manuel, hebdomadaire, PR touchant le banc ; non requis).
Résultat local : 32 réussis, 2 ignorés (tests propres à `ui_login` côté `basic_auth`), en `basic_auth` **et** `ui_login`, tests indépendants de l'ordre (vérifié aussi en ordre inversé). Les constats `ui_login` ci-dessous sont chacun couverts par une assertion.

## Routes et opérations constatées

| Besoin | Constat |
| --- | --- |
| Téléchargement CBZ | `GET /api/v1/chapter/{chapterId}/download` existe : 200, `Content-Type: application/vnd.comicbook+zip`, `Content-Disposition: attachment; filename="<manga> - <chapitre>.cbz"`, signature `PK`, octets identiques au fichier servi, archive ouverte par `zipfile`. Fichier absent : 400 (`Invalid folder to create CBZ…`) |
| Suppression | REST `DELETE /api/v1/manga/{mangaId}/chapter/{index}` et GraphQL `deleteDownloadedChapter(input:{id})` suppriment le fichier du disque |
| Équivalent GraphQL du téléchargement | **Aucun champ de `Query` ni de `Mutation` n'est nommé comme un téléchargement d'archive** (introspection des deux types, motifs `cbz`, `archive`, `downloadChapter`, `exportChapter`) : GraphQL offre `enqueueChapterDownload`, `deleteDownloadedChapter`, `fetchChapterPages`. Seule la route REST sert le CBZ (R7 : pas de repli GraphQL constaté pour l'archive) |
| Version | `aboutServer { name version buildType }` et `GET /api/v1/settings/about` concordent (`v2.4.2366`) |
| Rafraîchissement | `mutation fetchChapters(input:{mangaId})` voit un nouveau fichier déposé dans le dossier local |
| Réglages de mise à jour | `settings { globalUpdateInterval updateMangas excludeCompleted excludeNotStarted excludeUnreadChapters authMode downloadAsCbz }` |
| Bibliothèque, catégories | `updateManga(patch:{inLibrary})`, `createCategory`, `updateMangaCategories(patch:{addToCategories / removeFromCategories})` : OK |
| Source non chargée | `source(id:"…")` inconnu : erreur GraphQL (champ non nul), aucune donnée ; `extensions` et `extensionStores` vides, `sources` = `["0"]` seule |
| Basic | `basic_auth` : 401 sans identifiants ou avec mauvais mot de passe (REST et GraphQL) |
| ui_login | Supporté en v2.4.2366 : mutations `login` / `refreshToken` (JWT), REST en `Bearer` ; REST sans jeton 401, GraphQL sans jeton 200 avec erreur `Unauthorized` ; REST en Basic refusé (401) ; `aboutServer` reste lisible sans jeton |
| Identité (EF-SRC-036) | `sourceId` = `"0"`, `chapter.url` = `<dossier>/<fichier>.cbz` ; inchangés après redémarrage du conteneur (les ids locaux aussi, la base étant persistée) |
| Sans réseau | Le conteneur n'a aucune route sortante : sonde TCP vers une IP littérale, après contrôle que `bash`, `timeout` et la boucle locale fonctionnent, échoue avec le code 1 (erreur réseau explicite) ou 124 (délai) |

## Écarts avec la spec

1. **Le téléchargeur de Suwayomi ignore la source locale** (`Downloader.kt` : « Skip local source downloads »). Avec la seule
   source locale, un chapitre n'est jamais « téléchargé » et la route renvoie 400. Le banc simule le téléchargement en posant le
   CBZ synthétique là où le serveur le cherche (`downloads/mangas/Local source/<manga>/<chapitre>.cbz`). Ce qui est vérifié : la
   route, ses en-têtes et la suppression. **Non vérifié** (exige une extension tierce, interdite par ENF-LEG-013) : la production
   du CBZ par le téléchargeur à partir d'une source distante, et `isDownloaded` (reste `false` pour un fichier posé à la main : assertion).
2. **Dépôts d'extensions** : la v2.x les nomme `extensionStores` (variable `EXTENSION_STORES`, requêtes `extensionStores`,
   mutations `addExtensionStore`/`removeExtensionStore`) ; la spec parle de « dépôts ». Réglage non exposé dans `settings`.
3. **Comptes multiples** : `ui_login` v2.4.2366 n'a qu'un compte (celui de `AUTH_USERNAME`) ; aucune mutation de gestion
   d'utilisateurs ni de rôles. Non testables : permissions d'un nouveau compte, bibliothèque et catégories par compte,
   compteur de références des téléchargements partagés. À traiter en M1a si la fonction apparaît.
4. **Branche principale de Suwayomi** (exigée par la passe 5) : non jouée dans ce banc, qui reste sur la version épinglée ;
   un second job sur `latest` non bloquant est à ajouter si on veut le signal de dérive (ADR-0008, « nightly »).
5. **`safe_open_archive`** n'existe pas encore dans le code (M1a) ; l'ouverture est vérifiée avec `zipfile.testzip()`.
6. **Corpus de langues (ENF-I18N-005)** : la source locale n'expose que `localsourcelang` ; rien à relever sans extension.

## Conclusion

Le contrat REST de téléchargement CBZ, la suppression, l'authentification (Basic et JWT), les équivalents GraphQL de gestion
(hors archive) et la stabilité d'identité tiennent sur la version épinglée : **S5 passe**, avec les réserves 1, 3 et 4.
