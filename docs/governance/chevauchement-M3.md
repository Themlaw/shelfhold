# Revue de chevauchement : sortie de M3 (1.0)

Source : ENF-GOV-003, ENF-GOV-002 ; suite de la [revue de M0](chevauchement-M0.md). Faite le 2026-11-15 à partir des notes de version publiques et des titres de commits récents. **Ce n'est pas une analyse exhaustive** : seules les versions et les commits parus depuis la dernière revue (2026-11-04, c'est-à-dire depuis la 0.4) ont été lus, rien n'a été installé ni essayé.

## Sources

| Projet | Lu | Lien |
|---|---|---|
| Kavita | aucune version publiée depuis v0.9.1.4 (2026-09-02) ; titres des commits de la branche principale depuis le 2026-11-04 (numéros de version internes 0.9.1.20 à 0.9.1.23) | <https://github.com/Kareadita/Kavita> |
| Komga | 1.30.1 (2026-11-10) | <https://github.com/gotson/komga/releases> |
| Suwayomi-Server | aucune version publiée depuis v2.4.2366 (2026-09-30, version épinglée du banc de contrat) ; titres des commits depuis le 2026-11-04 | <https://github.com/Suwayomi/Suwayomi-Server> |
| BookOrbit | v3.7.0 (2026-11-08), v3.8.0 (2026-11-11) | <https://github.com/bookorbit/bookorbit/releases> |
| Uchiyomi | v0.106.0 à v0.108.1 (2026-11-09 à 2026-11-14) | <https://github.com/AngeloSha/uchiyomi/releases> |

## Constats

| Projet | Ce qu'il livre depuis la 0.4 | Effet sur le périmètre de Shelfhold |
|---|---|---|
| Kavita | Notation et critiques pour tous les lecteurs, lecture continue dans le lecteur PDF, annotations et corrections de la synchronisation Kobo (commits, pas de version publiée). | Aucune exigence retirée. Shelfhold ne lit toujours pas : la lecture reste dans Kavita en V1. À surveiller : la synchronisation Kobo de Kavita recoupe le futur `/lite` et le kosync propre de M5. |
| Komga | 1.30.1 : correctifs (en-têtes CSP de l'API, concurrence d'une table temporaire, interface « NextUI » et traductions). Rien de fonctionnel nouveau. | Aucune. Komga reste une destination (M4) et non une concurrente de l'atelier. |
| Suwayomi | Corrections et unification des entrées de chapitre des flux OPDS, filtrage des séries commencées, navigateurs intégrés fermés proprement (commits). | Le filtrage « commencées » touche le refus guidé d'EF-SRC-029 (`excludeUnreadChapters`, `excludeNotStarted`) : relire la sonde à la prochaine version épinglée. Le flux OPDS de Suwayomi ne change pas le rôle de Shelfhold : il ne range pas dans Kavita, ne protège pas un enfant et ne rend aucun âge. |
| BookOrbit | v3.7.0 : fusion de doublons et liaison d'éditions avec un plan relu (fichiers, progression et notes repris), récapitulatif annuel de lecture ; v3.8.0 : clés d'API dédiées. | Concurrent actif pour un serveur de lecture complet. Sa fusion avec plan et ses clés d'API rejoignent des choses que Shelfhold a déjà (fusion d'Éditions avec plan, clés d'API nommées) ; rien ne change, car BookOrbit est un autre serveur de lecture, non un pont vers une bibliothèque Kavita existante. |
| Uchiyomi | Bibliothèques privées par compte, images d'un dossier réunies en un chapitre, point de synchronisation de KOReader et OPDS, import d'une sauvegarde avec la progression, connexion « UI Login » de Suwayomi. | **Le recoupement le plus fort** : suivi, téléchargement, comptes, OPDS et progression dans un seul serveur. D'après ses notes, il ne range toujours pas dans une bibliothèque Kavita existante, n'a pas de contrôle parental par étagère avec plafond d'âge ni de retenue des titres non classés, et ne rend pas de plan de renommage. Sa progression de lecture et son import recoupent M4 (synchronisation) et M5 (OPDS et kosync propres). |

## Ce qui ne bouge pas, ce qui est à suivre

- **Rien n'est livré par un tiers à la place d'une exigence de la V1.** Aucune exigence ne passe en « Jamais » : le pont Suwayomi vers une bibliothèque Kavita existante, le contrôle parental par étagère, la copie conservée et les plans de renommage ne figurent dans aucune des notes lues (risque R24 du [registre de pré-mortem](pre-mortem.md) inchangé).
- **À suivre pour M4 et M5** : la synchronisation de progression d'Uchiyomi (partial MD5, OPDS) et la synchronisation Kobo de Kavita, avant d'écrire l'ADR « arbre et indexeurs » et le kosync propre. Ce qu'ils livrent sort du périmètre de ces lots, la même règle que pour Kavita, Komga et Suwayomi.
- **À relire à la prochaine version épinglée de Suwayomi** : la sonde d'EF-SRC-029 et le flux OPDS.
- Ni un nom de site de téléchargement ni un outil de contournement n'est cité ici, et la revue n'en recommande aucun.

## Relecture du registre de pré-mortem

[pre-mortem.md](pre-mortem.md) relu le 2026-11-15. Ligne 3 (« Kavita, Komga ou Suwayomi livrent la fonction ») : signal non déclenché. Les autres lignes n'ont pas de signal nouveau, hormis la vélocité, relevée dans [velocite.md](velocite.md).

## Décision

- Aucune exigence n'est déclarée livrée par un tiers : aucune passe en « Jamais ».
- Prochaine revue : sortie de M4.
