# Revue de chevauchement — sortie de M0

Source : ENF-GOV-002, ENF-GOV-003 ; [GOVERNANCE.md](https://github.com/Themlaw/shelfhold/blob/main/GOVERNANCE.md), [ADR-0020](../adr/0020-positionnement-atelier-kavita.md). Première revue, faite le 2026-11-04 à partir des notes de version publiques récentes. **Ce n'est pas une analyse exhaustive** : seules les trois dernières versions notables de chaque projet ont été lues, et les concurrents de la liste de veille ne sont jugés que sur leur description publique.

## Sources

| Projet | Versions lues | Lien |
|---|---|---|
| Kavita | v0.9.0 (2026-04-30), v0.9.1.0 (2026-08-28), v0.9.1.4 (2026-09-02) | <https://github.com/Kareadita/Kavita/releases> |
| Komga | 1.28.0 (2026-09-29), 1.29.0 (2026-10-12), 1.30.0 (2026-11-04) | <https://github.com/gotson/komga/releases> |
| Suwayomi-Server | v2.4.2366 (2026-09-30, version épinglée du banc de contrat) | <https://github.com/Suwayomi/Suwayomi-Server/releases> |
| BookOrbit | v3.6.0 (2026-10-28) | <https://github.com/bookorbit/bookorbit> |
| Uchiyomi | description du dépôt (pas de version publiée lue) | <https://github.com/AngeloSha/uchiyomi> |
| Tranga | dernière version publiée 1.5 (2023-06-20) ; dépôt toujours actif (dernier commit 2026-10-26) | <https://github.com/C9Glax/tranga> |
| Mangarr | dépôts `donderjoekel/Mangarr` (inactif depuis 2025-04) et forks | <https://github.com/donderjoekel/Mangarr> |

## Constats

| Projet | Ce qu'il livre | Effet sur le périmètre |
|---|---|---|
| Kavita | Durcissement de sécurité et performance (v0.9.0), couvertures et polices EPUB (v0.9.1), métadonnées Hardcover et Mangabaka réservées à Kavita+ (payant). | Aucune EF retirée. À surveiller pour les fournisseurs de métadonnées (M2, M4) : Kavita+ couvre Hardcover et Mangabaka, Shelfhold ne doit pas les dupliquer par défaut. Risque R24 inchangé (pas de pont depuis Suwayomi). |
| Komga | Nouvelle interface « NextUI » en bêta (édition en masse, entités notables, connexion OAuth2, sessions HTTP en base). | Aucune EF retirée. Komga reste une destination (M4), non une concurrente de l'atelier. À revoir pour OIDC (M5) et l'édition en masse. |
| Suwayomi | Synchronisation SyncYomi v2, mutation GraphQL de réordonnancement des téléchargements, `isDefaultCategory` « en préparation de changements incompatibles », et **« Skip LocalSource downloading »**. | Aucune EF retirée. Deux points à suivre : (1) les changements d'API de catégories annoncés touchent EF-SRC-029 (M1b) ; (2) le saut du téléchargement de la source locale est à rapprocher du banc S5, qui s'appuie sur cette source : le contrat est passé sur la version épinglée, à rejouer à chaque montée de version. |
| BookOrbit | Serveur de lecture actif (releases fréquentes, plugins). | Concurrent à suivre ; rien dans sa description ne couvre le pont Suwayomi vers Kavita. |
| Uchiyomi | Serveur et lecteur de manga avec téléchargeur (« Sonarr pour manga, avec lecteur »). | Concurrent partiel (suivi et téléchargement) ; ne range pas dans une bibliothèque Kavita existante d'après sa description. À suivre pour la porte P0. |
| Tranga, Mangarr | Téléchargeurs de manga automatiques ; Tranga sans version publiée depuis 2023 mais commits en 2026, Mangarr principal inactif (forks actifs). | Pas de menace pour M0 ; Tranga à resurveiller. |

## Relecture du registre de pré-mortem

[pre-mortem.md](pre-mortem.md) relu le 2026-11-04. Ligne 3 (« Kavita, Komga ou Suwayomi livrent la fonction ») : signal non déclenché, voir les constats ci-dessus ; date de revue mise à jour. Les autres lignes n'ont pas de signal nouveau à ce stade (aucune mesure de vélocité encore).

## Vélocité

[velocite.md](velocite.md) relu : le relevé de M0 est vide, aucune mesure d'effort agent n'a encore été consignée ; à renseigner à la clôture de M0, avec la projection de M1a (ENF-GOV-005).

## Décision

- Aucune exigence n'est déclarée livrée par un tiers à ce jour : aucune passe en « Jamais ».
- Question de la porte P0 (« un concurrent fait-il déjà le pont sur une bibliothèque Kavita existante ? ») : **non d'après ces sources documentaires** ; à confirmer par essai manuel de Uchiyomi et BookOrbit avant la clôture de M0.
- Prochaine revue : sortie de M1a.
