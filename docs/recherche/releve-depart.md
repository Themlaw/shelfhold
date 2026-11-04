# Relevé de départ (documentaire, générique)

Source : ENF-GOV-006, [ADR-0038](../adr/0038-validation-et-portes-de-decision.md). Ce relevé est une **recherche documentaire** sur la situation de départ d'un foyer type. Il ne décrit aucune installation réelle. Les **entretiens utilisateurs sont reportés** : ils dépendent du porteur et n'ont pas eu lieu (décision M0 9). L'exigence ENF-GOV-006 est donc déclarée PARTIELLE dans `tools/traceability-exceptions.toml`.

## Situation de départ d'un foyer type

- Un hôte unique (serveur domestique ou petit NAS) avec Docker.
- Un serveur de lecture, **Kavita**, qui scanne des dossiers organisés par bibliothèque (manga, livres, bandes dessinées).
- Un outil d'acquisition de manga ou de webtoon, **Suwayomi**, qui télécharge les chapitres dans son propre arbre de dossiers (`<Source>/<Titre>/<chapitre>`).
- Le lien entre les deux est manuel : monter le dossier de téléchargement de Suwayomi dans un dossier de Kavita, ou le renommer et le déplacer par script.
- D'autres contenus arrivent à la main : livres du domaine public, fichiers achetés sans mesure technique de protection.

## Ce que Shelfhold remplace

| Pratique actuelle | Limite documentée | Ce que Shelfhold met à la place |
|---|---|---|
| Montage direct du dossier Suwayomi dans le dossier de Kavita | Arborescence non conforme aux règles de nommage de Kavita : séries dupliquées, volumes ou chapitres mal reconnus | Rangement ordonné et nommage conforme à Kavita (jalon M1b) |
| Scripts planifiés (renommage, dédoublonnage, couvertures) | Fragiles, sans idempotence ni retour arrière, souvent écrits une seule fois | Export idempotent avec vérification et bascule ordonnée (M1a, M1b) |
| Métadonnées pauvres (`ComicInfo.xml` incohérent, sans âge ni titre de tri) | Kavita reconstruit séries et volumes de façon erratique ; restriction d'âge inopérante | `ComicInfo` normalisé (M1b) |
| Une interface par outil | Pas de recherche globale ni de vue unifiée | Interface d'atelier (M3) |
| Suivi de lecture éclaté entre appareils | Reprise difficile d'un appareil à l'autre | Synchronisation de progression (M4) |

## Métriques de départ à mesurer

Mesures locales, sans télémétrie, à réaliser sur une installation volontaire avant M1a. Seuls des agrégats sont publiés ici.

| Métrique | Unité | Méthode |
|---|---|---|
| Interventions manuelles de rangement par mois | nombre | Journal tenu pendant 4 semaines |
| Séries dupliquées ou mal reconnues dans Kavita | nombre et pourcentage | Comparaison de la liste des séries avec le dossier source |
| Délai entre la sortie d'un chapitre en amont et sa disponibilité dans Kavita | heures (médiane) | Relevé de dates sur un échantillon de séries suivies |
| Chapitres arrivés dans Kavita sans intervention | pourcentage | Journal de l'automatisation existante |
| Fichiers orphelins ou doublons dans le dossier de Kavita | nombre | Parcours du dossier |

Ces valeurs alimentent la porte P1 ([portes.md](../governance/portes.md)) : le pont doit tourner avec moins d'interventions manuelles par mois que ce relevé.

## Reste à faire (jalon M1a)

- Réaliser les entretiens courts auprès de foyers volontaires (notes privées, agrégats publiés ici).
- Remplacer les hypothèses ci-dessus par les mesures du tableau.
