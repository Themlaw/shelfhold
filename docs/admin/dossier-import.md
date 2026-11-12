# Dossier d'import

Le dossier d'import prend vos propres fichiers : CBZ, EPUB et PDF (EF-SRC-012). Chaque fichier est
lu, vérifié, rangé comme une Unité, puis **retiré du dossier seulement quand sa copie rangée a été
relue et reconnue identique** (même empreinte SHA-256). Au moindre doute, il reste où il est.

## Le mettre en route

Définissez `SHELFHOLD_IMPORT_DIR` : un dossier **distinct** de `/library`, `/store` et `/data` (et
qui n'est ni dedans ni au-dessus), sinon Shelfhold refuse de démarrer. Sans ce réglage, l'import
est éteint. Un passage automatique a lieu toutes les dix minutes ; à la main :

```
shelfhold import scan --dry-run    # liste ce qui est prêt, ne range ni ne retire rien
shelfhold import scan              # range ce qui est prêt
shelfhold import scan --purchase   # prend aussi le sous-dossier achats/ comme un achat déclaré
```

Chaque fichier devient une Œuvre, une Édition et une Unité, rangée par la barrière des nouvelles
Éditions comme tout ce qui arrive (une étagère sous 18+ ne la prend pas tant qu'elle n'est pas
classée).

## Ce que Shelfhold refuse, et ce qu'il dit

Un fichier refusé reste en place, intact, et reçoit un message. Rien n'est jamais modifié ni
déverrouillé.

| Cas | Message |
|---|---|
| Fichier protégé (chiffrement du contenu, document de droits, `/Encrypt` dans un PDF) | « Fichier protégé, non pris en charge. » |
| Fichier protégé par une licence de prêt ou d'achat | Message dédié : lisez-le dans l'application de sa licence, empruntez-le en médiathèque ou achetez une version sans protection. |
| Livre audio (reconnu à son contenu, quel que soit son nom) | Refusé tant qu'aucune édition audio n'existe. |
| Type inconnu | Seuls les CBZ, EPUB et PDF sont importés ; le type se lit dans le contenu. |
| Archive piégée, trop volumineuse, PDF qui est aussi une archive | Refusé, avec sa raison. |
| Lien symbolique, lien physique, autre disque monté dans le dossier | Refusé : déposez le fichier lui-même. |
| Nom piégé (`..`, caractère de contrôle ou de direction, espace ou point final, nom trop long) | Renommez le fichier. |
| Deux noms identiques une fois normalisés (accents, majuscules) | Les deux sont refusés : renommez l'un d'eux. |

Un PDF n'est rangé que s'il est **prouvé simple** : il est relu en mode strict dans un processus
isolé (mémoire et temps bornés) et doit s'ouvrir sans erreur, sans chiffrement, sans fichier joint
ni enveloppe d'un fichier protégé. Sinon (PDF abîmé, délai dépassé, doute), il est refusé avec un
message et reste en place : réexportez-le en PDF simple.

L'obfuscation de polices d'un EPUB n'est pas une protection : elle n'est pas refusée comme telle.

## Le retrait d'un fichier rangé

Le fichier n'est pas supprimé sous son nom (un nom peut être repris par une autre version entre
deux instants) : il est d'abord déplacé dans `.shelfhold-removing/`, comparé au fichier qui a été lu
(même disque, même inode), et supprimé seulement s'il est identique. Sinon il est remis en place.
Ce sous-dossier est ignoré par le passage (et verrouillé : deux passages ne se croisent pas) ; ce
qu'un arrêt brutal y laisse est remis dans son sous-dossier d'origine au passage suivant, puis relu comme un fichier ordinaire.

## Un fichier encore en cours d'écriture attend

Un fichier n'est lu que si sa taille et sa date de modification n'ont pas bougé entre deux
passages, et s'il a plus de quelques dizaines de secondes. Un nom en `.part`, `.tmp`,
`.crdownload` ou commençant par un point attend aussi. Rien à faire : il sera repris.

## Confiance, achat et tatouage

- Un import sans autre précision est **non fiable** (`untrusted`) : un EPUB passe par le
  neutraliseur et ne sort que sous sa forme neutralisée ; un PDF est rangé tel quel et **n'est pas
  exporté** (« À ranger », raison `content.untrusted_pdf`).
- Un import **déclaré comme un achat** est `publisher` : le fichier n'est jamais réécrit, et il est
  marqué `watermarked`. Seuls les fichiers du sous-dossier `achats/` peuvent l'être, et seulement
  quand vous lancez `shelfhold import scan --purchase` : le passage automatique les laisse en place
  (message « il n'est pris que si vous déclarez un achat ») au lieu de les prendre comme non
  fiables. Tout le reste du dossier est toujours non fiable.
- Une heuristique (entrées ou métadonnées typiques d'une copie personnalisée) peut aussi poser le
  drapeau `watermarked`. C'est une **suggestion** : elle ne refuse jamais un fichier, elle ne
  dispense jamais un EPUB non fiable du neutraliseur, et elle est toujours dite dans le résultat.
- Un fichier `watermarked` n'entre dans aucun paquet de diagnostic (seuls son empreinte et sa
  taille sont cités) et n'est jamais planifié vers une destination `egress = externe`. Un achat
  tatoué se lit dans le foyer, jamais au-delà.

## Ce qui garde l'original

Un EPUB non fiable est rangé sous forme de dérivé : sans `/store`, l'original ne serait conservé
nulle part. Le fichier reste alors dans le dossier d'import (`import.original_not_kept`) :
configurez `/store` pour que l'import puisse le retirer.

## Le dossier ne doit pas être Shelfhold lui-même

Un dossier d'import qui contient, ou est contenu dans, `/library`, `/store` ou `/data` est refusé :
Shelfhold reprendrait ce qu'il a exporté.

## Importer un arbre de téléchargements Suwayomi existant

`shelfhold import tree <chemin> [--dry-run]` crée les Unités d'un ancien dossier de
téléchargements Suwayomi (`mangas/<source>/<série>/<chapitre>`, un chapitre étant un CBZ ou un
dossier d'images), **sans rien retélécharger**. Donnez le chemin absolu du dossier qui contient
`mangas` (ou `mangas` lui-même).

- L'arbre est **lu seulement** : rien n'y est retiré ni écrit, même pas le sous-dossier de
  quarantaine du dossier d'import. Il reste donc utilisable par Suwayomi.
- Un chapitre est rattaché à la liaison Suwayomi **existante** de sa série, trouvée par le titre.
  Une série sans liaison est ignorée et dite (`import.tree_no_series`) : aucune Œuvre n'est créée
  d'après un nom de dossier, donc une Œuvre purgée ne revient jamais par cette voie. Suivez
  d'abord la série, puis relancez la commande.
- Un chapitre que le suivi connaissait déjà sans fichier reçoit le fichier dans sa propre Variante.
  Un numéro déjà rangé est un doublon : il est ignoré, jamais rangé en double.
- Les pierres tombales s'appliquent : une série ou un contenu purgé est refusé
  (`import.tombstoned`).
- Un dossier d'images est empaqueté en CBZ : les pages sont renommées dans l'ordre de lecture
  d'après ce que leurs octets montrent (jamais d'après leur nom), un seul `ComicInfo.xml` termine
  l'archive, et le résultat passe la liste blanche des CBZ (images reconnues par leurs octets)
  avant tout rangement. Une page qui n'est pas une image refuse tout le chapitre.
- Tout est `untrusted`, comme pour le dossier d'import. Liens symboliques, liens physiques,
  fichiers spéciaux, autre disque, noms piégés et fichiers encore en cours d'écriture sont
  refusés ou mis en attente comme pour le dossier d'import ; un fichier protégé reste en place
  avec son message.
