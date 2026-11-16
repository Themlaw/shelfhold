---
status: acceptée
date: 2026-11-16
decision-makers: mainteneur
---
# ADR-0054 — Normaliser le format des pages d'image (amende EF-EXP-046)

## Contexte et problème
Une source sert ses pages récentes en **AVIF**. Suwayomi les range telles quelles dans le CBZ et Kavita les transmet sans conversion. Le lecteur web les affiche, mais l'appli Android Kover (JPEG, PNG, GIF, WebP, BMP) et KOReader (MuPDF) montrent des **pages blanches**. Le porteur lit avec KOReader.

EF-EXP-046 disait « Kavita : pages en passthrough, couverture posée par l'API en JPEG, PNG ou WebP ». La couverture était traitée, pas les pages. Le porteur demande désormais la normalisation, à l'entrée de Suwayomi **et** de façon générale.

## Décision

### 1. Liste blanche, reconnue par les octets magiques
JPEG, PNG, GIF et WebP sont gardés tels quels. Le format d'une page se lit dans ses **octets magiques** (`infrastructure/content_trust/cbz_entries.py`, `sniff_page_kind`), jamais dans son extension : une page `.png` qui contient de l'AVIF est de l'AVIF, une page `.avif` qui contient du JPEG est du JPEG (elle garde ses octets, son extension devient vraie). Toute autre image est **réencodée** : AVIF, JPEG XL, HEIC/HEIF, BMP, TIFF, JPEG 2000.

### 2. Cible
JPEG qualité 90 par défaut, l'alpha aplati sur fond blanc, l'orientation EXIF appliquée, aucune métadonnée de l'amont conservée. Réglages : `SHELFHOLD_PAGE_TARGET_FORMAT` (`jpeg` | `png` | `webp`) et `SHELFHOLD_PAGE_TARGET_QUALITY` (1 à 100, pour JPEG et WebP). Jamais AVIF ni JPEG XL en sortie. Une destination qui déclare `accepted_image_types` restreint ce qu'elle prend telle quelle ; la cible est toujours un format qu'elle prend. La déclaration de Kavita passe à `{jpeg, png, webp, gif}` (elle valait pour les couvertures : elle vaut maintenant pour les pages, et Kavita compte les GIF).

### 3. Où
- **À l'entrée** : toute archive qui entre par le dossier d'import, l'import d'un arbre Suwayomi, les sources et Suwayomi (`PageFormatCbzStore`, devant `SvgCbzStore`, dans les deux compositions qui rangent un CBZ).
- **Aux assemblages** : le rendu part du fichier canonique, donc les pages d'un tome ou d'un webtoon assemblé (L10, « Plus tard » dans le plan) sont déjà normalisées ; le jour où L10 écrit un fichier, il passe par le même rendu.
- **À la sortie** : tout CBZ écrit dans une destination (`CbzRenderer.render`) et tout fichier que l'OPDS sert. Le bloc série de L49a est un zip stocké dont les tailles et les CRC sont connus avant le premier octet : il **ne convertit pas à la volée**, il lit les fichiers du dossier de destination, déjà normalisés par l'export. Un chapitre qui garde une page hors liste (une page illisible, un fichier écrit avant ce lot) est laissé de côté, comme avant.
- **Adoption** : un fichier adopté dans une bibliothèque Kavita n'est pas réécrit à l'adoption ; il l'est par la commande ci-dessous, qui réécrit aussi les fichiers déjà servis par une destination.

### 4. Copie canonique : l'original est gardé dans `/store` quand il est configuré
Un réencodage est **avec perte et sans retour**. Choix retenu (recommandation du porteur) : l'archive reçue est la copie canonique. Avec `/store` : l'original y est conservé **octet pour octet** avant toute réécriture, et `/library` garde la version normalisée, déclarée **dérivé** (`derived_hash`, `derived_by = page-normalizer@v1`) par le mécanisme qui sert déjà aux pages SVG (`KeepCopier.declare_derivative`). Les deux comptent comme copies indépendantes (EF-BIB-027, EF-BIB-045), la purge, les pierres tombales et la sauvegarde les connaissent déjà. **Sans `/store`**, rien n'est réécrit sur place : la bibliothèque garde l'archive reçue et la normalisation se fait **seulement à la sortie**. La commande refuse de réécrire une bibliothèque sans `/store` (`pages.store_not_configured`).

Pourquoi pas « normaliser seulement à la sortie » partout : la bibliothèque contiendrait des pages que ni Kover ni KOReader ne lisent, et chaque rendu referait le travail de décodage. Pourquoi pas « sans original » : perte irréversible d'un fichier que la source peut retirer.

Le rendu à la sortie part de la copie canonique. Il garde `content_hash` égal à celui de la source (c'est l'identité que le plan compare) ; `RenderedCbz.pages_normalized` dit que des pages ont été réencodées.

### 5. Comportement
- **Nom et ordre des pages gardés** ; seule l'extension change. Deux pages qui finiraient avec le même nom : celle qui porte déjà le nom le garde, l'autre devient `nom-1.jpg`, `nom-2.jpg`... ; l'ordre de l'archive ne change pas.
- `ComicInfo.xml` est recopié, en dernier (Kavita le lit en fin d'archive) ; `ImageSize`, `ImageWidth` et `ImageHeight` des seuls `<Page>` réencodés sont mis à jour, rien d'autre n'est touché, `PageCount` reste juste (aucune page n'est retirée). Les fichiers qui ne sont pas des images sont recopiés tels quels.
- **Tout ou rien** : un seul refus de page laisse l'archive telle qu'elle a été reçue et ne produit rien. Codes fermés : `page.empty` (0 octet : ce n'est pas une erreur de format, c'est dit), `page.undecodable`, `page.too_large`, `page.format_unsupported`. À l'entrée, l'archive est rangée telle quelle ; à la sortie, elle part telle quelle (un chapitre n'est pas perdu pour une page) ; l'OPDS la laisse de côté.
- **Écriture atomique et idempotente** : fichier de préparation puis renommage par fsops (`write_atomic`, relu avant de remplacer). Une archive déjà normalisée n'est pas réécrite (`unchanged`) : son hash ne bouge pas. Deux passes sur la même archive donnent les mêmes octets (zip déterministe).
- **Le décodage passe par le worker d'images existant** (`infrastructure/imaging`), par une nouvelle opération `reencode` : mêmes plafonds de pixels et d'octets (type `page_input`, 89 Mpx et 128 Mio), mémoire, CPU et délai bornés, pas de réseau, seccomp. Aucun second worker. Les pages déjà en liste blanche ne sont pas décodées (on ne dépense pas un processus par page d'une archive saine).
- **Commande** : `shelfhold library normalize-pages [--dry-run]`. Sans état, donc reprenable après un kill -9 : elle retrouve ce qui reste. `KeepCopier.declare_derivative` reprend une réécriture coupée entre le renommage et l'écriture en base (cas ajouté par ce lot).

### 6. Dépendances (vérifié le 2026-11-16)
- **AVIF** : lu nativement par Pillow 13 (libavif dans la roue ; le worker l'utilisait déjà pour les couvertures). **BMP, TIFF, JPEG 2000** : natifs aussi ; ils sont ajoutés à l'entrée du seul type `page_input`, jamais aux autres.
- **JPEG XL** : `pillow-jxl-plugin` 1.3.8 : roues manylinux 2,5 Mo (amd64) et 2,8 Mo (arm64), musllinux 9 à 10 Mo ; licence **non déclarée** dans les métadonnées PyPI.
- **HEIC/HEIF** : `pillow-heif` 1.8.1 : BSD-3-Clause, roues 5,7 Mo (amd64) et 6,4 Mo (arm64), musllinux 6,7 et 7,5 Mo ; embarque libheif et libde265 (LGPL-3, compatible AGPL) ; brevets HEVC.
- La taille ajoutée tient largement sous la limite de 300 Mo compressés. **Refus tout de même, proprement** : deux décodeurs natifs de plus lisent du contenu non fiable (surface d'attaque et sandbox sans fil d'exécution supplémentaire : `RLIMIT_NPROC = 1`, seccomp sans `clone` de processus, à valider pour libjxl et libde265), `uv.lock` est figé, la licence du plugin JXL n'est pas établie, HEVC pose une question de brevets. Une page JPEG XL ou HEIC est donc signalée `page.format_unsupported` et l'archive est gardée telle quelle. **Écart noté** ; à rouvrir avec un lot de dépendance (ajouter le plugin à `docs/dependencies.md`, un essai du worker et une relecture adverse du décodeur).

Rapport avec l'ADR-0052 : une destination dont `accepted_content_types` et `accepted_image_types` n'ont
aucun format en commun voit sa liaison refusée par le planificateur ; le rendu ne traite pas ce cas.

## Conséquences
- Le test `test_avif_first_page_cover_set_as_jpeg_or_webp` n'affirme plus que les pages restent en AVIF : elles sortent en JPEG.
- Les chapitres déjà servis par une destination se rattrapent par `normalize-pages` : le fichier est rendu de nouveau et remplace l'ancien au même chemin (mise à jour de même chemin, EF-EXP-016 : l'ancien va à la corbeille de sa racine, une lecture déjà faite est respectée). KOReader voit un nouveau document (son empreinte change : `document_alias`).
- Écarts assumés : une archive qui mêle pages SVG et pages à convertir reçoit d'abord le dérivé SVG (les pages AVIF y restent) ; la commande ne rattrape pas le dérivé déjà déclaré ; la condition de reprise suppose que l'original gardé dans `/store` contient des pages à convertir.
