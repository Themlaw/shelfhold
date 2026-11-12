# Révisions : ce que Shelfhold fait quand une source republie

Quand une source republie un chapitre ou un livre, Shelfhold enregistre une **révision** de la Variante sans
remplacer le fichier exporté, sauf dans trois cas décrits ci-dessous. Rien n'est jamais réécrit en masse. Ce
panorama renvoie aux pages de détail.

## Les quatre sorts d'une révision

| Cas | Ce qui se passe | Page |
|---|---|---|
| Meilleure version, version de l'équipe préférée, réacquisition après échec d'intégrité | Le fichier est remplacé **au même chemin** ; Kavita garde la progression, KOReader repart du début | [mise-a-niveau.md](mise-a-niveau.md) |
| Nouvelle version qui compte moins de la moitié des pages, ou `stubbed` | Mise en **quarantaine** : l'ancienne version reste exportée, `unit.revision_suspect` prévient | [revisions-en-attente.md](revisions-en-attente.md) |
| Révision d'avant M2, ou qui ne change qu'un champ de métadonnées | Entre dans la **file** `revisions` ; un champ que l'API Kavita accepte est poussé sans toucher au fichier | [revisions-en-attente.md](revisions-en-attente.md) |
| Chapitre retiré à la source | La copie locale reste ; le chapitre devient irremplaçable | [revisions-en-attente.md](revisions-en-attente.md) |

## Les commandes

```
shelfhold revisions list                              # la file, le nombre de fichiers déjà lus, le jeton du lot
shelfhold revisions apply --confirm <jeton>           # réécrit le lot affiché ; les Unités déjà lues sont ignorées
shelfhold revisions apply --confirm <jeton> --include-read
shelfhold revisions apply --force-suspect <identifiant>   # accepte une révision en quarantaine, une à la fois
```

`apply` est une **action réservée** (session récente ou CLI de l'hôte, jamais une clé d'API) et ne part pas sans le
jeton affiché par `list`. Le seuil de la quarantaine se règle avec `SHELFHOLD_REVISION_MIN_PAGE_RATIO` (0,5 par défaut).

## Pour une série qui change souvent

Un chapitre déjà **lu** n'est pas remplacé par défaut (« ne pas mettre à niveau les Unités lues ») : la position
KOReader serait perdue. Voir aussi [suivi-multi-sources.md](suivi-multi-sources.md) pour la mise à niveau tardive vers
une source de meilleur rang.
