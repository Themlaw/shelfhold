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
shelfhold import scan --purchase   # déclare ces fichiers comme un achat (voir plus bas)
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

L'obfuscation de polices d'un EPUB n'est pas une protection : elle n'est pas refusée comme telle.

## Un fichier encore en cours d'écriture attend

Un fichier n'est lu que si sa taille et sa date de modification n'ont pas bougé entre deux
passages, et s'il a plus de quelques dizaines de secondes. Un nom en `.part`, `.tmp`,
`.crdownload` ou commençant par un point attend aussi. Rien à faire : il sera repris.

## Confiance, achat et tatouage

- Un import sans autre précision est **non fiable** (`untrusted`) : un EPUB passe par le
  neutraliseur et ne sort que sous sa forme neutralisée ; un PDF est rangé tel quel et **n'est pas
  exporté** (« À ranger », raison `content.untrusted_pdf`).
- Un import **déclaré comme un achat** est `publisher` : le fichier n'est jamais réécrit, et il est
  marqué `watermarked`.
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
