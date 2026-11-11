# Révisions en attente et garde-fou de révision suspecte

Quand une source republie un chapitre, Shelfhold l'enregistre comme une **révision** de la Variante
sans remplacer le fichier exporté. Rien n'est jamais réécrit en masse.

## Révision suspecte (EF-BIB-024)

Une nouvelle version qui compte **moins de la moitié des pages** de la version rangée, ou que la
source donne comme `stubbed`, est mise en **quarantaine** : l'ancienne version reste exportée,
l'événement `unit.revision_suspect` prévient l'administrateur. Une version en quarantaine n'est
jamais la référence de la suivante : une troncature progressive est refusée comme une troncature
brutale. Le seuil se règle avec `SHELFHOLD_REVISION_MIN_PAGE_RATIO` (0,5 par défaut).

## Disponibilité (EF-BIB-009)

Le vocabulaire est ouvert : un mot que Shelfhold ne connaît pas est gardé tel quel et traité comme
`locked` (jamais exporté). Un chapitre `removed` à la source **garde sa copie locale** : rien n'est
supprimé, le chapitre devient irremplaçable.

## La file `revisions` (EF-BIB-043)

- `shelfhold revisions list` : les révisions en attente (celles d'avant M2 y entrent la première
  fois), le nombre de fichiers **déjà lus** dont la position KOReader repartirait du début, et le
  jeton du lot.
- Une révision qui ne change qu'un champ de métadonnées que l'API Kavita accepte est poussée par
  l'API (champ verrouillé par Shelfhold, jamais un verrou posé par un tiers), sans toucher au
  fichier. Ce qu'elle ne peut pas prendre rejoint la file « à réécrire ».
- `shelfhold revisions apply --confirm <jeton>` : demande la réécriture du lot (action réservée :
  session récente ou CLI de l'hôte, jamais une clé). Les Unités déjà lues sont ignorées, sauf
  `--include-read`. Sans jeton, ou avec un jeton périmé (la file a changé), rien ne part.
- Une révision en quarantaine n'est appliquée que par `revisions apply --force-suspect`, une action
  réservée distincte ; la route est `POST /api/v1/revisions/force`, il n'existe pas de drapeau
  `force` sur `/api/v1/revisions/apply`. La mise à niveau au même chemin refuse elle aussi une
  version en quarantaine sans ce droit.
