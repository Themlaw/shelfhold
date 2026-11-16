# Normaliser le format des pages

Kover et KOReader n'affichent que le JPEG, le PNG, le GIF et le WebP : une page AVIF ressort blanche.
Shelfhold laisse donc ces quatre formats tels quels (reconnus par leurs premiers octets, jamais par
l'extension) et réencode tout le reste (AVIF, BMP, TIFF, JPEG 2000). Le nom et l'ordre des pages sont
gardés, seule l'extension change ; `ComicInfo.xml` est recopié avec les tailles à jour (ADR-0054).

## Réglages

| Variable | Défaut | Effet |
|---|---|---|
| `SHELFHOLD_PAGE_TARGET_FORMAT` | `jpeg` | format d'arrivée : `jpeg`, `png` ou `webp` |
| `SHELFHOLD_PAGE_TARGET_QUALITY` | `90` | qualité (1 à 100) du JPEG et du WebP ; la transparence est aplatie sur blanc |

## Où la copie normalisée vit

Avec `/store` (mode **conserver une copie**), l'archive reçue y reste telle quelle et `/library`, que
Kavita lit, reçoit la version normalisée : les deux comptent comme copies indépendantes. Sans `/store`,
rien n'est réécrit sur place (une perte sans original n'est pas réversible) : la normalisation se fait
à la sortie, vers les destinations et dans les blocs OPDS.

## Rattraper une bibliothèque existante

```
shelfhold library normalize-pages --dry-run   # liste ce qui serait fait
shelfhold library normalize-pages             # le fait
```

La commande est reprenable après un arrêt brutal et ne refait pas ce qui est déjà normalisé. Sans
`/store` elle refuse (`pages.store_not_configured`).

## Ce qui est refusé

Une page vide, illisible ou trop grande (`page.empty`, `page.undecodable`, `page.too_large`), ou en
JPEG XL ou HEIC (`page.format_unsupported`), laisse **toute** l'archive telle que reçue et est signalée
dans le journal ; rien n'est converti à moitié.

## Chapitres mis de côté

Un chapitre dont une page est refusée n'est **jamais** envoyé tel quel (une page AVIF s'affiche en
blanc dans KOReader) : il reste dans la bibliothèque tel que reçu, hors de chaque destination, et la
page **Santé** l'indique (nombre de chapitres par code). Il n'est pas décodé de nouveau à chaque
cycle : un refus certain vaut pour ce contenu ; une page qui fait planter le décodeur
(`pages.worker_failed`) est redemandée après un recul (un jour par échec, une semaine au plus) ou
par `normalize-pages`. Un TIFF à plusieurs images est refusé (`page.format_unsupported`).

## Après la normalisation

Un bloc série déjà téléchargé sur la liseuse doit être **retéléchargé** : l'empreinte de chaque
chapitre change. Un chapitre isolé garde sa progression.
