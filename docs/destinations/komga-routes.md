# Routes Komga appelées (liste blanche)

Source unique des routes de l'API Komga que Shelfhold appelle (EF-EXP-020, ENF-SEC-013). Le
client (`backend/src/shelfhold/export/targets/komga/routes.py`) refuse toute autre route avant
d'émettre la requête ; un test hors ligne vérifie que ce tableau et le code disent la même chose
(`test_komga_client_calls_only_allowlisted_routes`), et le faux Komga des tests tient sa propre
liste, écrite à part. Ajouter une route, c'est modifier ce tableau, le code et le faux dans la
même PR.

**Il n'y a pas de scan par dossier.** Komga rescanne une **bibliothèque** : une série est un
dossier, et l'unité qu'il relit est la bibliothèque. Shelfhold regroupe donc les dossiers que
l'export a touchés par la bibliothèque dont la racine les contient (comparaison par composantes :
`/manga-extra` n'est pas sous `/manga`), attend que l'écriture soit calme (30 s sans nouveau
dossier) et demande **un** scan par bibliothèque, quel que soit le nombre de dossiers. Aucun
chemin de dossier ne figure dans la requête. Un dossier qu'aucune bibliothèque ne contient ne
déclenche aucun scan (jamais « toutes les bibliothèques »).

**Après une suppression.** Quand l'hôte a retiré un fichier d'une bibliothèque, Shelfhold la
scanne puis vide sa corbeille (`empty-trash`) : Komga oublie les livres qu'il ne retrouve plus,
et ses enregistrements de cette bibliothèque seulement. Une bibliothèque qui n'a rien perdu garde
sa corbeille. `empty-trash` ne supprime aucun fichier : la suppression de fichiers reste celle de
l'hôte (`can_purge` reste faux).

**Authentification.** Clé d'API d'un compte dédié, **administrateur** (Komga exige ce rôle pour
scanner), envoyée dans l'en-tête `X-API-Key`, jamais dans l'URL ni dans un corps. La clé est
scellée par le `SecretVault` dans la ligne de sa cible, détenue par le cœur et jamais transmise à
un plugin de destination. Ni les journaux, ni l'audit, ni le paquet de diagnostic, ni l'export
intégral ne la portent. Les routes de scan ne prennent aucun corps et un seul paramètre, `deep`.

| Méthode | Route | Usage | Jalon |
| --- | --- | --- | --- |
| GET | `/actuator/info` | Lire la version de Komga (`build.version`) ; exige un compte administrateur | M4 |
| GET | `/api/v2/users/me` | Savoir à quel compte appartient la clé (rôles) | M4 |
| GET | `/api/v1/libraries` | Lire les bibliothèques et leurs racines : relier un dossier à sa bibliothèque | M4 |
| POST | `/api/v1/libraries/{library_id}/scan` | Un scan de la bibliothèque, débouncé, réponse 202 ; exige un compte administrateur | M4 |
| POST | `/api/v1/libraries/{library_id}/empty-trash` | Vider la corbeille de la bibliothèque après une suppression, réponse 202 ; exige un compte administrateur | M4 |

`{library_id}` est un identifiant alphanumérique (jamais un chemin) : le client le vérifie avant
de construire la requête.

## Regroupement des séries

Komga prend le **dossier** pour la série (`series_grouping = folder`) ; Kavita lit le **nom**
(`name`). Le rendu commun de l'ADR-0052 les fait coïncider : `<racine>/<Série>/<Série> - …`, deux
niveaux, aucun sous-dossier de tomes. Sur un arbre qui ne le respecte pas, Komga ne voit pas ce
que voit Kavita : un sous-dossier de tomes devient une série de plus, un fichier à la racine un
« one-shot ». La destination le dit sans rien réécrire (`layout_conflicts` : `nested_series_folder`,
`file_at_library_root`, `name_differs_from_folder`).

## Ce que cette destination ne fait pas encore

- Aucun compte n'est audité : `access_partitioning = none`, donc **aucune étagère sous 18+** ne
  lui est liée (EF-EXP-047), pour sa propre liaison comme pour un arbre qu'elle indexe. L'audit
  des bibliothèques de chaque compte enfant et le plancher de version (1.22.0) sont ceux de L08b.
- Ni progression, ni collections, ni événements : `progress`, `collections` et `events` valent
  `none`.
